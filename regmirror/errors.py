"""Public error hierarchy for the regmirror package.

Error semantics (fixed public contract):

* ``ConfigError``       -- the requested scan is *impossible by construction*
                           (bad field/group/parameter configuration). Raised
                           before any device read is attempted.
* ``AddressError``      -- a register read lies outside the device image.
* ``DeviceReadError``   -- the device image did not honour ``read`` (raised,
                           wrong length, non-register word).
* ``InconsistentScanError`` -- the retry *budget* was exhausted while the
                           version gate kept failing. No decoded field values
                           are ever attached to this exception.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RegMirrorError",
    "ConfigError",
    "AddressError",
    "DeviceReadError",
    "InconsistentRead",
    "InconsistentScanError",
    "SnapshotUnavailableError",
    "MissingHistory",
    "MultiDeviceReadError",
    "DeviceFailure",
    "InconsistentMirrorScanError",
    "DeviceInconsistency",
]


class RegMirrorError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(RegMirrorError, ValueError):
    """The scan configuration is invalid; retrying cannot help.

    This is the "proven impossible" case: it is raised before any device
    access, distinct from :class:`InconsistentScanError` (budget exhausted).
    """


class AddressError(ConfigError):
    """A register address/range is outside the declared image."""


class DeviceReadError(RegMirrorError):
    """The device image violated the ``read(start, count)`` contract."""


@dataclass(frozen=True)
class InconsistentRead:
    """Diagnostic record for one group that failed the version gate.

    It deliberately contains only the two raw 16-bit version words; it never
    carries decoded field data, so a failed scan cannot leak partial values.
    """

    group: str
    version_before: int
    version_after: int

    def __str__(self) -> str:
        before = self.version_before
        after = self.version_after
        if before & 1 or after & 1:
            state = "odd (write-in-progress)"
        else:
            state = "changed"
        return (
            f"group {self.group!r}: version {before} -> {after} ({state})"
        )


class InconsistentScanError(RegMirrorError):
    """All attempts failed the atomicity gate (retry budget exhausted).

    Attributes:
        attempts: total attempts that were made (== ``max_attempts``).
        reasons:  one :class:`InconsistentRead` per failing group on the
                  last attempt.
    """

    def __init__(self, attempts: int, reasons: tuple[InconsistentRead, ...]):
        self.attempts = attempts
        self.reasons = tuple(reasons)
        detail = "; ".join(str(r) for r in self.reasons) or "no groups"
        super().__init__(
            f"snapshot inconsistent after {attempts} attempt(s): {detail}"
        )


@dataclass(frozen=True)
class MissingHistory:
    """Diagnostic for one device whose history does not reach a barrier.

    Like :class:`InconsistentRead`, it carries no field data -- only the
    requested barrier and the oldest sequence number the device retains.
    """

    device_id: str
    barrier: int
    oldest_seq: int

    def __str__(self) -> str:
        return (
            f"device {self.device_id!r}: no commit at or before barrier "
            f"{self.barrier} (oldest retained seq {self.oldest_seq})"
        )


class SnapshotUnavailableError(RegMirrorError):
    """``snapshot_unavailable``: the requested barrier predates retained
    history for at least one device.

    This is distinct from :class:`InconsistentScanError` (a live read that
    never stabilised) and from :class:`ConfigError` (an impossible
    request): the *barrier itself* is valid, but the bounded history no
    longer contains a commit that old.  No mirror is published.

    Attributes:
        barrier: the requested barrier.
        missing: one :class:`MissingHistory` per device with too-short
                 history.
    """

    #: Stable machine-readable status string (documented in the README).
    status = "snapshot_unavailable"

    def __init__(self, barrier: int, missing: tuple[MissingHistory, ...]):
        self.barrier = barrier
        self.missing = tuple(missing)
        detail = "; ".join(str(m) for m in self.missing) or "no devices"
        super().__init__(
            f"snapshot_unavailable at barrier {barrier}: {detail}"
        )


@dataclass(frozen=True)
class DeviceFailure:
    """One device whose ``read`` violated its contract during a mirror scan."""

    device_id: str
    error: DeviceReadError

    def __str__(self) -> str:
        return f"device {self.device_id!r}: {self.error}"


@dataclass(frozen=True)
class DeviceInconsistency:
    """One device whose live read never passed the version gate."""

    device_id: str
    attempts: int
    reason: InconsistentRead

    def __str__(self) -> str:
        return (
            f"device {self.device_id!r}: inconsistent after {self.attempts} "
            f"attempt(s) ({self.reason})"
        )


class MultiDeviceReadError(RegMirrorError):
    """One or more devices violated the ``read`` contract during a mirror.

    Attributes:
        failures: one :class:`DeviceFailure` per failing device.
    """

    #: Stable machine-readable status string (documented in the README).
    status = "device_read_error"

    def __init__(self, failures: tuple[DeviceFailure, ...]):
        self.failures = tuple(failures)
        detail = "; ".join(str(f) for f in self.failures)
        super().__init__(f"device read failure(s): {detail}")


class InconsistentMirrorScanError(RegMirrorError):
    """At least one device exhausted its retry budget during a mirror.

    Attributes:
        failures: one :class:`DeviceInconsistency` per failing device.
    """

    #: Stable machine-readable status string (documented in the README).
    status = "mirror_inconsistent"

    def __init__(self, failures: tuple[DeviceInconsistency, ...]):
        self.failures = tuple(failures)
        detail = "; ".join(str(f) for f in self.failures)
        super().__init__(f"mirror inconsistent: {detail}")
