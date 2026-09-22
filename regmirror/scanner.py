"""Atomic snapshot scanner over a versioned register image.

Protocol (fixed public contract):

* Each group owns one 16-bit *version register*.  The device sets it
  **odd** while writing the group's data registers and **even** when the
  group is stable again.  Every committed write increments the version
  by 2 (mod 2**16), so versions wrap around.
* One attempt = read version, read the data registers in chunks of at
  most ``ScanConfig.max_read_words``, read version again.  The attempt
  commits iff both version words are equal and even.
* Failed attempts are retried up to ``max_attempts`` in total; then
  :class:`InconsistentScanError` is raised.  The exception carries only
  diagnostic version words — never decoded field data — so a failed
  scan yields a complete snapshot or no values at all.

Version-wraparound contract: because the version is 16-bit and advances
by 2 per commit, equal even versions guarantee "no commit happened"
only if the device commits at most 32767 times during a single scan
(``MAX_COMMITS_PER_SCAN``).  32768 commits would alias to the same
version word; that is a deployment contract, not something the scanner
can detect.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .device import check_read_result
from .errors import (
    AddressError,
    ConfigError,
    InconsistentRead,
    InconsistentScanError,
)
from .fields import FieldSpec, FieldValue, decode_field

__all__ = [
    "MAX_COMMITS_PER_SCAN",
    "RegisterGroup",
    "ScanConfig",
    "GroupSnapshot",
    "Snapshot",
    "Scanner",
]

#: Deployment contract: maximum device commits during one scan for the
#: version-equality test to be sound (2**16 / 2 - 1).
MAX_COMMITS_PER_SCAN = 32767


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class RegisterGroup:
    """A named set of fields guarded by one 16-bit version register."""

    name: str
    version_address: int
    fields: tuple[FieldSpec, ...]

    def __init__(
        self,
        name: str,
        version_address: int,
        fields: Sequence[FieldSpec],
    ):
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"group name must be a non-empty str, got {name!r}"
            )
        version_address = _check_int("version_address", version_address)
        if not 0 <= version_address <= 0xFFFF:
            raise ConfigError(
                f"group {name!r}: version_address {version_address} "
                "outside 0..65535"
            )
        fields = tuple(fields)
        if not fields:
            raise ConfigError(f"group {name!r}: at least one field required")
        for f in fields:
            if not isinstance(f, FieldSpec):
                raise ConfigError(
                    f"group {name!r}: fields must be FieldSpec, "
                    f"got {type(f).__name__}"
                )
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise ConfigError(
                f"group {name!r}: duplicate field names {sorted(names)}"
            )
        # Reject overlapping register ranges instead of silently decoding
        # ambiguous data.
        spans = sorted((f.address, f.last_address, f.name) for f in fields)
        for (lo1, hi1, n1), (lo2, hi2, n2) in zip(spans, spans[1:]):
            if lo2 <= hi1:
                raise ConfigError(
                    f"group {name!r}: fields {n1!r} and {n2!r} overlap "
                    f"at register {lo2}"
                )
        for f in fields:
            if f.address <= version_address <= f.last_address:
                raise ConfigError(
                    f"group {name!r}: version register {version_address} "
                    f"overlaps field {f.name!r}"
                )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version_address", version_address)
        object.__setattr__(self, "fields", fields)

    @property
    def data_span(self) -> tuple[int, int]:
        """Inclusive [first, last] register addresses of all fields."""
        return (
            min(f.address for f in self.fields),
            max(f.last_address for f in self.fields),
        )


@dataclass(frozen=True)
class ScanConfig:
    """Tuning for one scan run."""

    max_attempts: int = 4
    max_read_words: int = 16

    def __post_init__(self) -> None:
        max_attempts = _check_int("max_attempts", self.max_attempts)
        if not 1 <= max_attempts <= 1_000_000:
            raise ConfigError(
                f"max_attempts must be in 1..1000000, got {max_attempts}"
            )
        max_read_words = _check_int("max_read_words", self.max_read_words)
        if not 1 <= max_read_words <= 65536:
            raise ConfigError(
                f"max_read_words must be in 1..65536, got {max_read_words}"
            )


@dataclass(frozen=True)
class GroupSnapshot:
    """Committed, mutually consistent field values of one group."""

    name: str
    version: int  # the even version word this snapshot was committed under
    attempts: int  # attempts this group needed (1 = clean first try)
    values: tuple[FieldValue, ...]

    def __getitem__(self, field_name: str) -> FieldValue:
        for v in self.values:
            if v.name == field_name:
                return v
        raise KeyError(field_name)


@dataclass(frozen=True)
class Snapshot:
    """A complete, all-groups-committed scan result."""

    groups: tuple[GroupSnapshot, ...]

    def __getitem__(self, group_name: str) -> GroupSnapshot:
        for g in self.groups:
            if g.name == group_name:
                return g
        raise KeyError(group_name)

    def __len__(self) -> int:
        return len(self.groups)


class Scanner:
    """Scans register groups off a device image with the version gate."""

    def __init__(self, config: ScanConfig | None = None):
        self.config = config if config is not None else ScanConfig()
        if not isinstance(self.config, ScanConfig):
            raise ConfigError(
                f"config must be a ScanConfig, got {type(self.config).__name__}"
            )

    # -- public ---------------------------------------------------------

    def scan(
        self, device: object, groups: Sequence[RegisterGroup]
    ) -> Snapshot:
        """Return a complete snapshot or raise InconsistentScanError.

        ``device`` must provide ``read(start, count)`` and ``size``.
        All configuration is validated before the first device read, so
        a misconfigured scan never touches the device.
        """
        groups = self._validate(device, groups)
        committed: list[GroupSnapshot] = []
        for group in groups:
            committed.append(self._scan_group(device, group))
        return Snapshot(tuple(committed))

    # -- internals ------------------------------------------------------

    def _validate(
        self, device: object, groups: Sequence[RegisterGroup]
    ) -> tuple[RegisterGroup, ...]:
        if isinstance(groups, (str, bytes)) or not isinstance(groups, Sequence):
            raise ConfigError("groups must be a sequence of RegisterGroup")
        groups = tuple(groups)
        if not groups:
            raise ConfigError("at least one group is required")
        for g in groups:
            if not isinstance(g, RegisterGroup):
                raise ConfigError(
                    f"groups must contain RegisterGroup, got {type(g).__name__}"
                )
        names = [g.name for g in groups]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate group names {sorted(names)}")
        read = getattr(device, "read", None)
        if not callable(read):
            raise ConfigError(
                f"device must provide read(start, count), "
                f"got {type(device).__name__}"
            )
        size = getattr(device, "size", None)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ConfigError(
                f"device must expose a positive int size, got {size!r}"
            )
        for g in groups:
            first, last = g.data_span
            if last >= size:
                raise AddressError(
                    f"group {g.name!r}: data registers {first}..{last} "
                    f"outside device image of size {size}"
                )
            if g.version_address >= size:
                raise AddressError(
                    f"group {g.name!r}: version register "
                    f"{g.version_address} outside device image of size {size}"
                )
        return groups

    def _read(self, device: object, start: int, count: int) -> tuple[int, ...]:
        return check_read_result(start, count, device.read(start, count))

    def _scan_group(
        self, device: object, group: RegisterGroup
    ) -> GroupSnapshot:
        first, last = group.data_span
        chunk = self.config.max_read_words
        last_reason: InconsistentRead | None = None
        for _attempt in range(1, self.config.max_attempts + 1):
            v1 = self._read(device, group.version_address, 1)[0]
            data: dict[int, int] = {}
            addr = first
            while addr <= last:
                n = min(chunk, last - addr + 1)
                words = self._read(device, addr, n)
                data.update(zip(range(addr, addr + n), words))
                addr += n
            v2 = self._read(device, group.version_address, 1)[0]
            if v1 == v2 and not (v1 & 1):
                values = tuple(
                    decode_field(
                        f,
                        tuple(data[a] for a in range(f.address, f.last_address + 1)),
                    )
                    for f in group.fields
                )
                return GroupSnapshot(group.name, v1, _attempt, values)
            last_reason = InconsistentRead(group.name, v1, v2)
        assert last_reason is not None  # max_attempts >= 1
        raise InconsistentScanError(self.config.max_attempts, (last_reason,))
