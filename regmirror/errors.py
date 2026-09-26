"""Public error hierarchy for the regmirror package.

Error semantics (fixed public contract):

* ``ConfigError``       -- the requested scan is *impossible by construction*
                           (bad field/group/parameter configuration). Raised
                           before any device read is attempted.
* ``AddressError``      -- a register read lies outside the device image.
* ``DeviceReadError``   -- the device image did not honour ``read`` (raised,
                           wrong length, non-register word).
* ``PlanConflictError`` -- a write plan is impossible by construction:
                           overlapping masks demand opposite bit values
                           (or an entry is malformed). Raised before any
                           device access.
* ``VersionConflictError`` -- a batch write was rejected by the simulated
                           device because the version register no longer
                           matched the plan. Nothing was written.
* ``PrepareRetriesExhausted`` -- building the plan could never observe a
                           stable (even, unchanged) version within the
                           attempt budget.
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
    "PlanConflictError",
    "VersionConflictError",
    "PrepareRetriesExhausted",
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


class PlanConflictError(ConfigError):
    """A write plan is self-contradictory or malformed.

    Raised *before* any device access (same "proven impossible" family as
    :class:`ConfigError`): e.g. two items in one batch overlap on a bit
    but require different final values, or a requested value sets bits
    outside its mask.
    """


class VersionConflictError(RegMirrorError):
    """The compare-and-write batch was rejected: the version moved.

    Attributes:
        expected_version: even version word the plan was prepared against.
        observed_version: version word the device held at submit time.

    The whole batch is refused atomically -- no register is modified.  A
    blind retry with the stale plan is never performed; the caller must
    re-read and prepare a new plan.
    """

    def __init__(self, expected_version: int, observed_version: int):
        self.expected_version = expected_version
        self.observed_version = observed_version
        super().__init__(
            f"compare-and-write rejected: expected version "
            f"{expected_version}, device holds {observed_version}"
        )


class PrepareRetriesExhausted(RegMirrorError):
    """No stable version was observed while preparing a write plan."""

    def __init__(self, attempts: int, version_before: int, version_after: int):
        self.attempts = attempts
        self.version_before = version_before
        self.version_after = version_after
        super().__init__(
            f"write plan could not be prepared after {attempts} "
            f"attempt(s): version {version_before} -> {version_after}"
        )


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
