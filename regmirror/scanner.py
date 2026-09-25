"""Atomic snapshot scanner over a versioned register image.

Protocol (fixed public contract):

* Each group owns one 16-bit *version register*.  The device sets it
  **odd** while writing the group's data registers and **even** when the
  group is stable again.  Every committed write increments the version
  by 2 (mod 2**16), so versions wrap around.
* One attempt = read version, read the group's data registers using the
  precomputed :class:`~regmirror.planner.GroupPlan` (a minimum number of
  reads of at most ``ScanConfig.max_read_words`` words each, bridging at
  most ``ScanConfig.max_gap_words`` extra registers and never touching
  the group's ``forbidden_ranges``), read version again.  The attempt
  commits iff both version words are equal and even.  Version reads are
  always separate singleton reads -- the version check is never merged
  into a data block.
* Failed attempts are retried up to ``max_attempts`` in total; then
  :class:`InconsistentScanError` is raised.  On failure the whole group
  is retried using the same plan from the beginning; blocks read on the
  failed attempt are never reused.  The exception carries only diagnostic
  version words — never decoded field data — so a failed scan yields a
  complete snapshot or no values at all.

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
from .planner import (
    GroupPlan,
    ScanPlan,
    normalize_forbidden_ranges,
    plan_group,
)

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
    """A named set of fields guarded by one 16-bit version register.

    Fields are non-overlapping by default; pass ``allow_overlap=True`` to
    deliberately group fields that share registers (the planner covers the
    union of their registers exactly once).  ``forbidden_ranges`` declares
    half-open ``[start, end)`` intervals this group's reads must never
    touch -- an empty tuple means nothing is off-limits.
    """

    name: str
    version_address: int
    fields: tuple[FieldSpec, ...]
    allow_overlap: bool = False
    forbidden_ranges: tuple[tuple[int, int], ...] = ()

    def __init__(
        self,
        name: str,
        version_address: int,
        fields: Sequence[FieldSpec],
        *,
        allow_overlap: bool = False,
        forbidden_ranges: Sequence[tuple[int, int]] = (),
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
        if not isinstance(allow_overlap, bool):
            raise ConfigError(
                f"group {name!r}: allow_overlap must be a bool, "
                f"got {type(allow_overlap).__name__}"
            )
        # Reject overlapping register ranges unless the caller explicitly
        # opts in; without the flag ambiguous data is a configuration bug.
        spans = sorted((f.address, f.last_address, f.name) for f in fields)
        for (lo1, hi1, n1), (lo2, hi2, n2) in zip(spans, spans[1:]):
            if lo2 <= hi1 and not allow_overlap:
                raise ConfigError(
                    f"group {name!r}: fields {n1!r} and {n2!r} overlap "
                    f"at register {lo2}"
                )
        forbidden = normalize_forbidden_ranges(forbidden_ranges)
        for f in fields:
            if f.address <= version_address <= f.last_address:
                raise ConfigError(
                    f"group {name!r}: version register {version_address} "
                    f"overlaps field {f.name!r}"
                )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version_address", version_address)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "allow_overlap", allow_overlap)
        object.__setattr__(self, "forbidden_ranges", forbidden)

    @property
    def data_span(self) -> tuple[int, int]:
        """Inclusive [first, last] register addresses of all fields."""
        return (
            min(f.address for f in self.fields),
            max(f.last_address for f in self.fields),
        )


@dataclass(frozen=True)
class ScanConfig:
    """Tuning for one scan run.

    ``max_gap_words`` caps how many *extra* (non-field) registers a single
    data read may span to merge neighbouring fields; the default 65535
    preserves the legacy behaviour ("merge across any gap as long as the
    block fits in ``max_read_words``").
    """

    max_attempts: int = 4
    max_read_words: int = 16
    max_gap_words: int = 65535

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
        max_gap_words = _check_int("max_gap_words", self.max_gap_words)
        if not 0 <= max_gap_words <= 65535:
            raise ConfigError(
                f"max_gap_words must be in 0..65535, got {max_gap_words}"
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
        All configuration is validated -- and the read plan is built --
        before the first device read, so a misconfigured scan never
        touches the device.
        """
        groups = self._validate(device, groups)
        # Build the whole plan up front: an impossible layout (field in
        # a forbidden hole, span larger than max_read_words, ...) is a
        # ConfigError and must not cost a single device read.
        scan_plan = self.build_plan(device, groups)
        committed: list[GroupSnapshot] = []
        for group, plan in zip(groups, scan_plan.groups):
            committed.append(self._scan_group(device, group, plan))
        return Snapshot(tuple(committed))

    def build_plan(
        self, device: object, groups: Sequence[RegisterGroup]
    ) -> ScanPlan:
        """Return the read plan this scanner would execute.

        Pure planning: never calls ``device.read``.  Inspecting the plan
        is the supported way to see how many data reads each group gets
        and which extra registers get pulled in to merge fields; each
        group's own ``forbidden_ranges`` are honoured.
        """
        groups = tuple(groups)
        if isinstance(groups, (str, bytes)) or not isinstance(groups, Sequence):
            raise ConfigError("groups must be a sequence of RegisterGroup")
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
        size = getattr(device, "size", None)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ConfigError(
                f"device must expose a positive int size, got {size!r}"
            )
        return ScanPlan(
            tuple(
                plan_group(
                    g,
                    size=size,
                    max_read_words=self.config.max_read_words,
                    max_gap_words=self.config.max_gap_words,
                    forbidden_ranges=g.forbidden_ranges,
                )
                for g in groups
            )
        )

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
        self,
        device: object,
        group: RegisterGroup,
        plan: GroupPlan,
    ) -> GroupSnapshot:
        last_reason: InconsistentRead | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            # Every attempt executes the same fixed plan from scratch:
            # version read, all planned data blocks in ascending order,
            # version read.  A failed attempt is discarded wholesale --
            # blocks from a previous attempt are never reused.
            v1 = self._read(device, group.version_address, 1)[0]
            data: dict[int, int] = {}
            for block in plan.blocks:
                words = self._read(device, block.start, block.count)
                data.update(
                    zip(
                        range(block.start, block.start + block.count),
                        words,
                    )
                )
            v2 = self._read(device, group.version_address, 1)[0]
            if v1 == v2 and not (v1 & 1):
                values = tuple(
                    decode_field(
                        f,
                        tuple(
                            data[a]
                            for a in range(f.address, f.last_address + 1)
                        ),
                    )
                    for f in group.fields
                )
                return GroupSnapshot(group.name, v1, attempt, values)
            last_reason = InconsistentRead(group.name, v1, v2)
        assert last_reason is not None  # max_attempts >= 1
        raise InconsistentScanError(self.config.max_attempts, (last_reason,))
