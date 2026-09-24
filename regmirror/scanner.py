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

Barrier mirror protocol (``Scanner.mirror``): each target device keeps
an immutable history of commits with dense sequence numbers.  For the
requested barrier the scanner resolves every device's *last commit with
sequence <= barrier*, pins it (a pinned commit is exempt from bounded
history pruning), then reads all of its groups out of that one frozen
image.  New device commits during the reads therefore cannot mix into a
field.  If any device has no commit at or before the barrier, or the
needed commit was pruned, :class:`SnapshotUnavailableError` is raised
and no partial mirror is published.
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
    SnapshotUnavailableError,
)
from .fields import FieldSpec, FieldValue, decode_field

__all__ = [
    "MAX_COMMITS_PER_SCAN",
    "RegisterGroup",
    "ScanConfig",
    "GroupSnapshot",
    "Snapshot",
    "DeviceTarget",
    "DeviceSnapshot",
    "Mirror",
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


def _decode_group_data(
    group: "RegisterGroup", data: dict[int, int]
) -> tuple[FieldValue, ...]:
    return tuple(
        decode_field(
            f,
            tuple(data[a] for a in range(f.address, f.last_address + 1)),
        )
        for f in group.fields
    )


@dataclass(frozen=True)
class DeviceTarget:
    """One device plus the groups to mirror off it at a barrier.

    ``device`` must implement the history protocol (``read_at(seq,
    start, count)``, ``pin``/``unpin``, ``resolve_barrier``,
    ``retains``) and expose a positive int ``size`` -- as
    :class:`~regmirror.device.HistoryDevice` does.
    """

    name: str
    device: object
    groups: tuple[RegisterGroup, ...]

    def __init__(
        self,
        name: str,
        device: object,
        groups: Sequence[RegisterGroup],
    ):
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"device name must be a non-empty str, got {name!r}"
            )
        if device is None:
            raise ConfigError(f"device target {name!r}: device is required")
        groups = tuple(groups)
        if not groups:
            raise ConfigError(
                f"device target {name!r}: at least one group required"
            )
        for g in groups:
            if not isinstance(g, RegisterGroup):
                raise ConfigError(
                    f"device target {name!r}: groups must contain "
                    f"RegisterGroup, got {type(g).__name__}"
                )
        names = [g.name for g in groups]
        if len(set(names)) != len(names):
            raise ConfigError(
                f"device target {name!r}: duplicate group names "
                f"{sorted(names)}"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "groups", groups)


@dataclass(frozen=True)
class DeviceSnapshot:
    """All groups of one device decoded from one pinned commit.

    ``sequence`` is the sequence of the device's last commit at or
    before the requested barrier.  The values are a frozen view of that
    commit; later device commits cannot alter them.
    """

    name: str
    sequence: int
    groups: tuple[GroupSnapshot, ...]

    def __getitem__(self, group_name: str) -> GroupSnapshot:
        for g in self.groups:
            if g.name == group_name:
                return g
        raise KeyError(group_name)

    def __len__(self) -> int:
        return len(self.groups)


@dataclass(frozen=True)
class Mirror:
    """A complete cross-device barrier mirror: every device or nothing."""

    barrier: int
    devices: tuple[DeviceSnapshot, ...]

    def __getitem__(self, device_name: str) -> DeviceSnapshot:
        for d in self.devices:
            if d.name == device_name:
                return d
        raise KeyError(device_name)

    def __len__(self) -> int:
        return len(self.devices)

    @property
    def sequences(self) -> dict[str, int]:
        """Device name -> committed sequence served for the barrier."""
        return {d.name: d.sequence for d in self.devices}


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
                values = _decode_group_data(group, data)
                return GroupSnapshot(group.name, v1, _attempt, values)
            last_reason = InconsistentRead(group.name, v1, v2)
        assert last_reason is not None  # max_attempts >= 1
        raise InconsistentScanError(self.config.max_attempts, (last_reason,))

    # -- barrier mirror --------------------------------------------------

    def mirror(
        self, targets: Sequence[DeviceTarget], barrier: int
    ) -> Mirror:
        """Return one cross-device mirror at ``barrier`` or raise.

        For every target the device's *last commit with sequence <=
        barrier* is selected and pinned; all of its groups are decoded
        from that single frozen commit, so updates committed while the
        mirror is being assembled can never mix into a field.  If any
        device cannot serve the barrier (no commit yet, or the needed
        commit was pruned), :class:`SnapshotUnavailableError` is raised
        and no partial mirror is published.

        All configuration is validated before any history access.
        """
        targets = self._validate_targets(targets, barrier)
        resolved: list[tuple[DeviceTarget, int]] = []
        pinned: list[tuple[object, int]] = []
        try:
            # Phase 1: resolve and pin before reading any data words.
            for t in targets:
                dev = t.device
                try:
                    seq = dev.resolve_barrier(barrier)
                    dev.pin(seq)
                except SnapshotUnavailableError as exc:
                    raise SnapshotUnavailableError(
                        t.name, barrier, exc.reason, exc.oldest
                    ) from exc
                pinned.append((dev, seq))
                resolved.append((t, seq))
            # Phase 2: decode every group from its pinned frozen commit.
            device_snapshots: list[DeviceSnapshot] = []
            for t, seq in resolved:
                try:
                    group_snapshots = tuple(
                        self._read_historical_group(t.device, g, seq)
                        for g in t.groups
                    )
                    # Defensive recheck: a conforming history device cannot
                    # prune a pinned commit; a foreign one that does must
                    # fail the whole mirror rather than publish half data.
                    if not t.device.retains(seq):
                        raise SnapshotUnavailableError(
                            None, barrier, "history_pruned", None
                        )
                except SnapshotUnavailableError as exc:
                    raise SnapshotUnavailableError(
                        t.name, barrier, exc.reason, exc.oldest
                    ) from exc
                device_snapshots.append(
                    DeviceSnapshot(t.name, seq, tuple(group_snapshots))
                )
        finally:
            for dev, seq in pinned:
                dev.unpin(seq)
        return Mirror(barrier, tuple(device_snapshots))

    def _read_historical_group(
        self, device: object, group: RegisterGroup, seq: int
    ) -> GroupSnapshot:
        first, last = group.data_span
        chunk = self.config.max_read_words
        data: dict[int, int] = {}
        addr = first
        while addr <= last:
            n = min(chunk, last - addr + 1)
            words = check_read_result(
                addr, n, device.read_at(seq, addr, n)
            )
            data.update(zip(range(addr, addr + n), words))
            addr += n
        version_word = check_read_result(
            group.version_address,
            1,
            device.read_at(seq, group.version_address, 1),
        )[0]
        # attempts=0 marks a group served from pinned history without the
        # live retry gate.
        return GroupSnapshot(
            group.name, version_word, 0, _decode_group_data(group, data)
        )

    def _validate_targets(
        self, targets: Sequence[DeviceTarget], barrier: object
    ) -> tuple[DeviceTarget, ...]:
        if isinstance(targets, (str, bytes)) or not isinstance(
            targets, Sequence
        ):
            raise ConfigError("targets must be a sequence of DeviceTarget")
        targets = tuple(targets)
        if not targets:
            raise ConfigError("at least one device target is required")
        for t in targets:
            if not isinstance(t, DeviceTarget):
                raise ConfigError(
                    "targets must contain DeviceTarget, "
                    f"got {type(t).__name__}"
                )
        names = [t.name for t in targets]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate device names {sorted(names)}")
        if isinstance(barrier, bool) or not isinstance(barrier, int):
            raise ConfigError(
                f"barrier must be an int, got {type(barrier).__name__}"
            )
        if barrier < 0:
            raise ConfigError(f"barrier must be >= 0, got {barrier}")
        for t in targets:
            dev = t.device
            for meth in ("read_at", "pin", "unpin", "resolve_barrier",
                         "retains"):
                if not callable(getattr(dev, meth, None)):
                    raise ConfigError(
                        f"device {t.name!r}: {type(dev).__name__} does not "
                        f"provide history method {meth}()"
                    )
            size = getattr(dev, "size", None)
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise ConfigError(
                    f"device {t.name!r}: must expose a positive int size, "
                    f"got {size!r}"
                )
            for g in t.groups:
                first, last = g.data_span
                if last >= size:
                    raise AddressError(
                        f"device {t.name!r} group {g.name!r}: data "
                        f"registers {first}..{last} outside image of "
                        f"size {size}"
                    )
                if g.version_address >= size:
                    raise AddressError(
                        f"device {t.name!r} group {g.name!r}: version "
                        f"register {g.version_address} outside image of "
                        f"size {size}"
                    )
        return targets
