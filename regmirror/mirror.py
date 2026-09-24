"""Barrier-scoped atomic mirror scans across several versioned devices.

This module builds on the single-device version gate in :mod:`regmirror.scanner`:

* Every synthetic device owns a :class:`~regmirror.device.CommitStore` --
  an append-only history of immutable, sequence-numbered stable commits
  (``CommitRecord(seq, version, full_register_image)``) with optional
  bounded retention.
* A :class:`MirrorRequest` names several devices, one field group each,
  and one **barrier** (a global logical commit sequence number).
* :meth:`MirrorScanner.snapshot_at` returns, for **every** device, the
  fields of the last commit whose ``seq <= barrier`` -- or publishes
  nothing.

Why mid-read updates cannot mix inside one field:

* A commit record older than the device's newest commit is an immutable
  full-image copy; its fields are decoded straight from that copy, with
  **no device reads at all**, so an ongoing device write cannot touch it.
* When the selected commit *is* the device's current newest commit, the
  scanner performs the normal gated read (version -> chunked data ->
  version) and additionally requires the read words to be byte-for-byte
  equal to the historical record.  A torn field therefore fails both the
  version gate and the content comparison and is retried.

Failure policy (all-or-nothing, no partial mirror):

* barrier older than every retained commit of any device ->
  :class:`SnapshotUnavailableError` (``status = "snapshot_unavailable"``),
  raised before any device read;
* any device violates its ``read`` contract ->
  :class:`MultiDeviceReadError`;
* any device exhausts the retry budget ->
  :class:`InconsistentMirrorScanError`.

In every failure case no :class:`MirrorSnapshot` is returned and the
errors carry only diagnostics, never decoded field values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .device import check_read_result
from .errors import (
    AddressError,
    ConfigError,
    DeviceFailure,
    DeviceInconsistency,
    DeviceReadError,
    InconsistentRead,
    InconsistentMirrorScanError,
    MissingHistory,
    MultiDeviceReadError,
    SnapshotUnavailableError,
)
from .fields import FieldValue, decode_field
from .scanner import RegisterGroup, ScanConfig

__all__ = [
    "DeviceTarget",
    "MirrorRequest",
    "DeviceSnapshot",
    "MirrorSnapshot",
    "MirrorScanner",
]


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an int, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class DeviceTarget:
    """One (device, field group) pair participating in a mirror."""

    device_id: str
    device: object
    group: RegisterGroup


@dataclass(frozen=True)
class MirrorRequest:
    """A request for a barrier-scoped mirror of several devices.

    Parameters:
        name:    non-empty mirror name (used in the result).
        barrier: global logical seq; the result uses each device's last
                 commit with ``seq <= barrier`` (must be >= 0).
        devices: ``{device_id: device}``; each device must expose
                 ``size`` (positive int), ``read(start, count)``,
                 ``version_address`` (int) and a commit ``history``
                 (:class:`CommitStore`).
        groups:  ``{device_id: RegisterGroup}``; each group's
                 ``version_address`` must equal the device's.
    """

    name: str
    barrier: int
    targets: tuple[DeviceTarget, ...]

    def __init__(
        self,
        name: str,
        barrier: int,
        devices: Mapping[str, object],
        groups: Mapping[str, RegisterGroup],
    ):
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"mirror name must be a non-empty str, got {name!r}"
            )
        barrier = _check_int("barrier", barrier)
        if barrier < 0:
            raise ConfigError(f"barrier must be >= 0, got {barrier}")
        if not isinstance(devices, Mapping):
            raise ConfigError(
                "devices must be a mapping {device_id: device}, got "
                f"{type(devices).__name__}"
            )
        if not isinstance(groups, Mapping):
            raise ConfigError(
                "groups must be a mapping {device_id: RegisterGroup}, got "
                f"{type(groups).__name__}"
            )
        if not devices:
            raise ConfigError("at least one device is required")
        if set(devices) != set(groups):
            only_dev = sorted(set(devices) - set(groups))
            only_grp = sorted(set(groups) - set(devices))
            raise ConfigError(
                f"device/group key mismatch; devices only: {only_dev}, "
                f"groups only: {only_grp}"
            )
        targets: list[DeviceTarget] = []
        for device_id, dev in devices.items():
            if not isinstance(device_id, str) or not device_id:
                raise ConfigError(
                    f"device id must be a non-empty str, got {device_id!r}"
                )
            group = groups[device_id]
            if not isinstance(group, RegisterGroup):
                raise ConfigError(
                    f"group for device {device_id!r} must be a RegisterGroup, "
                    f"got {type(group).__name__}"
                )
            targets.append(DeviceTarget(device_id, dev, group))
        if len({t.device_id for t in targets}) != len(targets):
            raise ConfigError("duplicate device ids in request")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "barrier", barrier)
        object.__setattr__(self, "targets", tuple(targets))


@dataclass(frozen=True)
class DeviceSnapshot:
    """The committed fields of one device at the selected barrier commit."""

    device_id: str
    group_name: str
    seq: int
    version: int
    attempts: int  # live gate attempts used; 0 when served from history
    source: str  # "history" (immutable record) or "live" (gated device read)
    values: tuple[FieldValue, ...]

    def __getitem__(self, field_name: str) -> FieldValue:
        for v in self.values:
            if v.name == field_name:
                return v
        raise KeyError(field_name)


@dataclass(frozen=True)
class MirrorSnapshot:
    """Complete mirror: one :class:`DeviceSnapshot` per requested device."""

    name: str
    barrier: int
    status: str
    devices: tuple[DeviceSnapshot, ...]

    def __getitem__(self, device_id: str) -> DeviceSnapshot:
        for d in self.devices:
            if d.device_id == device_id:
                return d
        raise KeyError(device_id)

    def __len__(self) -> int:
        return len(self.devices)


class MirrorScanner:
    """Publishes barrier-scoped, all-or-nothing mirrors of several devices."""

    def __init__(self, config: ScanConfig | None = None):
        self.config = config if config is not None else ScanConfig()
        if not isinstance(self.config, ScanConfig):
            raise ConfigError(
                f"config must be a ScanConfig, got {type(self.config).__name__}"
            )

    # -- public ---------------------------------------------------------

    def snapshot_at(self, request: MirrorRequest) -> MirrorSnapshot:
        """Return the complete barrier mirror, or raise without partial data."""
        if not isinstance(request, MirrorRequest):
            raise ConfigError(
                "request must be a MirrorRequest, got "
                f"{type(request).__name__}"
            )
        self._validate_devices(request)
        barrier = request.barrier

        # History lookup needs no device reads; do it for every device
        # before touching any device, so insufficient history fails the
        # whole mirror as snapshot_unavailable with zero side effects.
        missing: list[MissingHistory] = []
        for t in request.targets:
            record = t.device.history.latest_at_or_before(barrier)
            if record is None:
                missing.append(
                    MissingHistory(t.device_id, barrier, t.device.history.oldest_seq)
                )
        if missing:
            raise SnapshotUnavailableError(barrier, tuple(missing))

        results: list[DeviceSnapshot] = []
        read_failures: list[DeviceFailure] = []
        inconsistencies: list[DeviceInconsistency] = []
        for t in request.targets:
            try:
                results.append(
                    self._scan_device(t, barrier)
                )
            except SnapshotUnavailableError as exc:
                # History can become too short mid-scan if a bounded
                # store prunes the target record as the device advances.
                missing.extend(exc.missing)
            except DeviceReadError as exc:
                read_failures.append(DeviceFailure(t.device_id, exc))
            except _BudgetExhausted as exc:
                inconsistencies.append(
                    DeviceInconsistency(
                        t.device_id, exc.attempts, exc.reason
                    )
                )
        if missing:
            raise SnapshotUnavailableError(barrier, tuple(missing))
        if read_failures:
            raise MultiDeviceReadError(tuple(read_failures))
        if inconsistencies:
            raise InconsistentMirrorScanError(tuple(inconsistencies))
        return MirrorSnapshot(
            request.name, barrier, "published", tuple(results)
        )

    # -- internals ------------------------------------------------------

    def _validate_devices(self, request: MirrorRequest) -> None:
        for t in request.targets:
            dev = t.device
            read = getattr(dev, "read", None)
            if not callable(read):
                raise ConfigError(
                    f"device {t.device_id!r} must provide read(start, count), "
                    f"got {type(dev).__name__}"
                )
            size = getattr(dev, "size", None)
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise ConfigError(
                    f"device {t.device_id!r} must expose a positive int size, "
                    f"got {size!r}"
                )
            version_address = getattr(dev, "version_address", None)
            if (
                isinstance(version_address, bool)
                or not isinstance(version_address, int)
            ):
                raise ConfigError(
                    f"device {t.device_id!r} must expose int version_address"
                )
            if version_address != t.group.version_address:
                raise ConfigError(
                    f"device {t.device_id!r}: device version_address "
                    f"{version_address} != group version_address "
                    f"{t.group.version_address}"
                )
            history = getattr(dev, "history", None)
            if not callable(getattr(history, "latest_at_or_before", None)):
                raise ConfigError(
                    f"device {t.device_id!r} history must provide "
                    "latest_at_or_before()"
                )
            oldest_seq = getattr(history, "oldest_seq", "missing")
            if oldest_seq != "missing" and not (
                oldest_seq is None or isinstance(oldest_seq, int)
            ):
                raise ConfigError(
                    f"device {t.device_id!r} history.oldest_seq must be an "
                    f"int or None, got {type(oldest_seq).__name__}"
                )
            first, last = t.group.data_span
            if last >= size:
                raise AddressError(
                    f"device {t.device_id!r}: group {t.group.name!r} data "
                    f"registers {first}..{last} outside image of size {size}"
                )
            if version_address >= size:
                raise AddressError(
                    f"device {t.device_id!r}: version register "
                    f"{version_address} outside image of size {size}"
                )

    def _read(
        self, device: object, start: int, count: int
    ) -> tuple[int, ...]:
        return check_read_result(start, count, device.read(start, count))

    def _scan_device(self, target: DeviceTarget, barrier: int) -> DeviceSnapshot:
        """Resolve one device, gated-reads only while the commit is live."""
        device = target.device
        group = target.group
        chunk = self.config.max_read_words
        first, last = group.data_span
        attempt = 0
        last_reason: InconsistentRead | None = None
        while True:
            # Re-resolve on every attempt: the device may commit (and a
            # bounded store may even prune) while retries are in flight.
            record = device.history.latest_at_or_before(barrier)
            if record is None:
                raise SnapshotUnavailableError(
                    barrier,
                    (MissingHistory(
                        target.device_id, barrier, device.history.oldest_seq
                    ),),
                )
            newest = device.history.newest
            if record.seq < newest.seq:
                # The wanted commit is sealed history: an immutable full
                # image; decode it without any device read.
                return self._decode_record(target, record, attempt, "history")

            attempt += 1
            v1 = self._read(device, group.version_address, 1)[0]
            data: dict[int, int] = {}
            addr = first
            while addr <= last:
                n = min(chunk, last - addr + 1)
                words = self._read(device, addr, n)
                data.update(zip(range(addr, addr + n), words))
                addr += n
            v2 = self._read(device, group.version_address, 1)[0]

            content_ok = (
                v1 == record.version
                and tuple(data[a] for a in range(first, last + 1))
                == tuple(record.words[a] for a in range(first, last + 1))
            )
            if v1 == v2 and not (v1 & 1) and content_ok:
                values = tuple(
                    decode_field(
                        f,
                        tuple(data[a] for a in range(f.address, f.last_address + 1)),
                    )
                    for f in group.fields
                )
                return DeviceSnapshot(
                    target.device_id, group.name, record.seq, record.version,
                    attempt, "live", values,
                )
            last_reason = InconsistentRead(group.name, v1, v2)
            if attempt >= self.config.max_attempts:
                raise _BudgetExhausted(attempt, last_reason)

    @staticmethod
    def _decode_record(
        target: DeviceTarget,
        record: object,
        attempts: int,
        source: str,
    ) -> DeviceSnapshot:
        group = target.group
        values = tuple(
            decode_field(
                f,
                tuple(
                    record.words[a]
                    for a in range(f.address, f.last_address + 1)
                ),
            )
            for f in group.fields
        )
        return DeviceSnapshot(
            target.device_id, group.name, record.seq, record.version,
            attempts, source, values,
        )


class _BudgetExhausted(Exception):
    """Internal signal carrying the last gate diagnostic for one device."""

    def __init__(self, attempts: int, reason: InconsistentRead):
        self.attempts = attempts
        self.reason = reason
        super().__init__(f"budget exhausted after {attempts} attempt(s)")
