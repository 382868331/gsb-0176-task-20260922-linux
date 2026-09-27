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
    "ScanStateError",
    "InconsistentRead",
    "InconsistentScanError",
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


class ScanStateError(RegMirrorError):
    """A resumable scan session was used from an invalid state."""


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
