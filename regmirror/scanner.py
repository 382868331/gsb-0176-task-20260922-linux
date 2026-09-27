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
from dataclasses import dataclass, field

from .device import check_read_result
from .errors import (
    AddressError,
    ConfigError,
    ScanStateError,
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
    "ScanSession",
    "PublishedImage",
    "UpdateResult",
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


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of :meth:`PublishedImage.try_update`.

    On failure ``snapshot`` is the image that remains published, which is
    ``None`` for a publisher that has never received a successful scan.
    """

    updated: bool
    snapshot: Snapshot | None
    error: InconsistentScanError | None = None


class _PauseRequested(Exception):
    """Internal control-flow signal at a safe read boundary."""


class ScanSession:
    """A scan that can be paused at device-read boundaries and resumed.

    Pause requests are cooperative: call :meth:`request_pause` (for example
    from a scripted device wrapper) and the session stops before its next
    device read.  Partial words are held only in the session; they are never
    published.  On resume the current version is re-read before reusing any
    words from before the pause.
    """

    def __init__(
        self,
        scanner: "Scanner",
        device: object,
        groups: Sequence[RegisterGroup],
    ):
        self._scanner = scanner
        self._device = device
        self._groups = scanner._validate(device, groups)
        self.status = "ready"
        self._group_index = 0
        self._attempt = 1
        self._version_before: int | None = None
        self._next_data_address = 0
        self._data: dict[int, int] = {}
        self._committed: list[GroupSnapshot] = []
        self._reasons: list[InconsistentRead] = []
        self._pause_requested = False
        self._result: Snapshot | None = None
        self._error: InconsistentScanError | None = None

    @property
    def result(self) -> Snapshot | None:
        """Complete snapshot, available only after successful completion."""
        return self._result

    @property
    def error(self) -> InconsistentScanError | None:
        """Retry-budget error, available only after failure."""
        return self._error

    def request_pause(self) -> None:
        """Ask the session to stop at the next safe read boundary."""
        if self.status in {"complete", "failed"}:
            raise ScanStateError(f"cannot pause a {self.status} session")
        self._pause_requested = True

    def run(self) -> Snapshot | None:
        """Run until completion, failure, or a requested pause.

        Returns the complete snapshot on success, or ``None`` if the run
        stopped because it was paused.
        """
        if self.status not in {"ready", "paused", "running"}:
            raise ScanStateError(f"cannot run a {self.status} session")
        previous_status = self.status
        self.status = "running"
        try:
            while self._group_index < len(self._groups):
                if self._version_before is None:
                    self._resume_at_group_boundary(previous_status == "paused")
                else:
                    if previous_status == "paused":
                        self._resume_mid_attempt()
                        if self._version_before is None:
                            previous_status = "running"
                            continue
                self._read_attempt_data()
                self._finish_attempt()
                previous_status = "running"
        except _PauseRequested:
            self.status = "paused"
            return None
        except InconsistentScanError as exc:
            self.status = "failed"
            self._error = exc
            raise
        self._result = Snapshot(tuple(self._committed))
        self.status = "complete"
        return self._result

    def _checkpoint(self) -> None:
        if self._pause_requested:
            self._pause_requested = False
            raise _PauseRequested

    def _device_read(self, start: int, count: int) -> tuple[int, ...]:
        self._checkpoint()
        return self._scanner._read(self._device, start, count)

    def _begin_group_attempt(self) -> None:
        group = self._groups[self._group_index]
        first, _last = group.data_span
        self._data = {}
        self._next_data_address = first
        self._version_before = self._device_read(group.version_address, 1)[0]

    def _resume_at_group_boundary(self, paused: bool) -> None:
        changed = (
            self._validate_committed_groups() if paused and self._group_index else []
        )
        if not changed:
            self._begin_group_attempt()
        else:
            self._restart_after_invalid_pause(changed[0])

    def _validate_committed_groups(self) -> list[InconsistentRead]:
        """Return changed groups completed before a pause."""
        versions = {
            group.name: self._device_read(group.version_address, 1)[0]
            for group in self._groups[: self._group_index]
        }
        return [
            InconsistentRead(snapshot.name, snapshot.version, versions[snapshot.name])
            for snapshot in self._committed
            if snapshot.version != versions[snapshot.name]
            or versions[snapshot.name] & 1
        ]

    def _resume_mid_attempt(self) -> None:
        group = self._groups[self._group_index]
        current = self._device_read(group.version_address, 1)[0]
        if current != self._version_before or (current & 1):
            reason = InconsistentRead(
                group.name, self._version_before or 0, current
            )
            self._retry_or_fail(reason)
            self._data = {}
            self._version_before = None

    def _restart_after_invalid_pause(self, reason: InconsistentRead) -> None:
        self._retry_or_fail(reason)
        self._group_index = 0
        self._committed = []
        self._begin_group_attempt()

    def _read_attempt_data(self) -> None:
        group = self._groups[self._group_index]
        _first, last = group.data_span
        chunk = self._scanner.config.max_read_words
        addr = self._next_data_address
        if addr > last:
            return
        while addr <= last:
            n = min(chunk, last - addr + 1)
            words = self._device_read(addr, n)
            self._data.update(zip(range(addr, addr + n), words))
            addr += n
            self._next_data_address = addr

    def _finish_attempt(self) -> None:
        group = self._groups[self._group_index]
        v2 = self._device_read(group.version_address, 1)[0]
        assert self._version_before is not None
        if self._version_before == v2 and not (v2 & 1):
            values = tuple(
                decode_field(
                    field,
                    tuple(
                        self._data[a]
                        for a in range(field.address, field.last_address + 1)
                    ),
                )
                for field in group.fields
            )
            self._committed.append(
                GroupSnapshot(
                    group.name, v2, self._attempt, values
                )
            )
            self._group_index += 1
            self._version_before = None
            self._data = {}
            self._reasons = []
            self._attempt = 1
            return

        reason = InconsistentRead(group.name, self._version_before, v2)
        self._retry_or_fail(reason)
        self._data = {}
        self._version_before = None

    def _retry_or_fail(self, reason: InconsistentRead) -> None:
        self._reasons = [reason]
        if self._attempt >= self._scanner.config.max_attempts:
            raise InconsistentScanError(self._attempt, (reason,))
        self._attempt += 1


@dataclass
class PublishedImage:
    """In-memory publisher that swaps only to a complete snapshot."""

    current: Snapshot | None = field(default=None)
    last_error: InconsistentScanError | None = field(default=None)

    def publish(self, snapshot: Snapshot) -> None:
        if not isinstance(snapshot, Snapshot):
            raise ConfigError(
                f"snapshot must be a Snapshot, got {type(snapshot).__name__}"
            )
        self.current = snapshot
        self.last_error = None

    def try_update(
        self,
        scanner: "Scanner",
        device: object,
        groups: Sequence[RegisterGroup],
    ) -> UpdateResult:
        """Attempt an update; retain the previous complete image on failure."""
        previous = self.current
        try:
            snapshot = scanner.scan(device, groups)
        except InconsistentScanError as exc:
            self.current = previous
            self.last_error = exc
            return UpdateResult(False, previous, exc)
        self.current = snapshot
        self.last_error = None
        return UpdateResult(True, snapshot, None)


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
        session = ScanSession(self, device, groups)
        return session.run()

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

    def start_session(
        self, device: object, groups: Sequence[RegisterGroup]
    ) -> ScanSession:
        """Create a pause/resume-capable scan session."""
        return ScanSession(self, device, groups)
