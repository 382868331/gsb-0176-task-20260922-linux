"""Version-gated batched bit writes (compare-version-and-write).

This module adds the write side of the register mirror: a *write plan*
is a batch of bit updates, each given as (register address, mask, new
bit value).  The flow is deliberately two-phase:

1. prepare_write_plan reads the version register and the affected data
   registers off the mirror, gated by the version (both reads of the
   version must agree and be even).  From that same-version image it
   computes, per register, the merged mask, the expected old word and
   the new word; unselected bits are carried over unchanged.
2. commit_write_plan hands the whole batch to the device's
   compare_version_and_write in one call.  If the version moved in the
   meantime the device rejects the *entire* batch -- no register is
   ever written half-way.

There is no automatic retry of a rejected plan: a stale plan object is
consumed by the commit attempt and cannot be committed again.  The
caller must re-read the mirror (a fresh prepare_write_plan) before
trying again, so a commit never blindly overwrites bits someone else
just wrote.

Overlapping items inside one batch are merged; items that demand
opposite values for the same bit are rejected with ConfigError
*before* any device read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .device import check_read_result
from .errors import (
    AddressError,
    ConfigError,
    InconsistentRead,
    InconsistentScanError,
)

__all__ = [
    "BitWrite",
    "PlannedWrite",
    "WritePlan",
    "prepare_write_plan",
    "commit_write_plan",
]

#: Group label used in InconsistentScanError diagnostics of this module.
_PLAN_GROUP = "write-plan"


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


def _check_word(name: str, value: object) -> int:
    value = _check_int(name, value)
    if not 0 <= value <= 0xFFFF:
        raise ConfigError(f"{name} must be a 16-bit word (0..65535), got {value}")
    return value


@dataclass(frozen=True)
class BitWrite:
    """One requested bit update: set the bits of mask to value.

    value carries the new bit values *under* the mask; bits of value
    outside mask are rejected rather than silently ignored.  mask must
    be non-zero.
    """

    address: int
    mask: int
    value: int

    def __post_init__(self) -> None:
        address = _check_int("address", self.address)
        if not 0 <= address <= 0xFFFF:
            raise ConfigError(
                f"address {address} outside 0..65535"
            )
        mask = _check_word("mask", self.mask)
        if mask == 0:
            raise ConfigError("mask must be non-zero")
        value = _check_word("value", self.value)
        if value & ~mask:
            raise ConfigError(
                f"value 0x{value:04X} has bits outside mask 0x{mask:04X}"
            )


@dataclass(frozen=True)
class PlannedWrite:
    """One merged register update inside a prepared plan."""

    address: int
    mask: int  # merged mask of all batch items touching this register
    old: int  # expected current word, read under the plan version
    new: int  # (old & ~mask) | (merged value & mask)


@dataclass
class WritePlan:
    """A prepared batch, bound to the version it was computed from.

    Not safe to commit twice: commit_write_plan consumes the plan
    whether the device accepts or rejects it.  Prepare a fresh plan
    from a fresh read instead of retrying a stale one.
    """

    version_address: int
    version: int  # the even version word this plan was computed from
    writes: tuple[PlannedWrite, ...]
    _consumed: bool = field(default=False, repr=False, compare=False)

    def __getitem__(self, address: int) -> PlannedWrite:
        for w in self.writes:
            if w.address == address:
                return w
        raise KeyError(address)

    def __len__(self) -> int:
        return len(self.writes)


def _merge_items(items: tuple[BitWrite, ...]) -> dict[int, tuple[int, int]]:
    """Merge items per address into {address: (mask, value)}.

    Compatible overlaps (same bits, same values) fold together; a
    contradiction on any shared bit is a ConfigError raised before any
    device access.
    """
    merged: dict[int, tuple[int, int]] = {}
    for item in items:
        if item.address in merged:
            mask0, value0 = merged[item.address]
            overlap = mask0 & item.mask
            if (value0 & overlap) != (item.value & overlap):
                raise ConfigError(
                    f"conflicting bit values at register {item.address}: "
                    f"mask 0x{mask0:04X}/value 0x{value0:04X} vs "
                    f"mask 0x{item.mask:04X}/value 0x{item.value:04X} "
                    f"disagree on bits 0x{overlap:04X}"
                )
            merged[item.address] = (
                mask0 | item.mask,
                (value0 & mask0) | (item.value & item.mask),
            )
        else:
            merged[item.address] = (item.mask, item.value & item.mask)
    return merged


def _validate_device(device: object) -> int:
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
    return size


def prepare_write_plan(
    device: object,
    version_address: int,
    items: Sequence[BitWrite],
) -> WritePlan:
    """Read the mirror under a stable version and compute a write plan.

    All configuration is validated (including overlap-conflict
    detection) before the first device read.  The version register and
    the affected data registers are then read through the version gate
    (version -> data -> version); if the two version words differ or
    are odd, InconsistentScanError is raised and no plan is produced --
    the caller may simply call prepare again to re-read.
    """
    version_address = _check_int("version_address", version_address)
    if not 0 <= version_address <= 0xFFFF:
        raise ConfigError(
            f"version_address {version_address} outside 0..65535"
        )
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ConfigError("items must be a sequence of BitWrite")
    items = tuple(items)
    if not items:
        raise ConfigError("at least one BitWrite is required")
    for it in items:
        if not isinstance(it, BitWrite):
            raise ConfigError(
                f"items must contain BitWrite, got {type(it).__name__}"
            )
    merged = _merge_items(items)  # conflict check before any device read
    size = _validate_device(device)
    if version_address >= size:
        raise AddressError(
            f"version register {version_address} outside device image "
            f"of size {size}"
        )
    for address in merged:
        if address >= size:
            raise AddressError(
                f"register {address} outside device image of size {size}"
            )

    def read(start: int, count: int) -> tuple[int, ...]:
        return check_read_result(start, count, device.read(start, count))

    addresses = sorted(merged)
    v1 = read(version_address, 1)[0]
    old_words = {a: read(a, 1)[0] for a in addresses}
    v2 = read(version_address, 1)[0]
    if v1 != v2 or v1 & 1:
        raise InconsistentScanError(1, (InconsistentRead(_PLAN_GROUP, v1, v2),))
    writes = tuple(
        PlannedWrite(
            address=a,
            mask=merged[a][0],
            old=old_words[a],
            new=(old_words[a] & ~merged[a][0]) | merged[a][1],
        )
        for a in addresses
    )
    return WritePlan(version_address, v1, writes)


def commit_write_plan(device: object, plan: WritePlan) -> int:
    """Commit a prepared plan atomically; return the new version.

    The plan is consumed by this call in either outcome.  If the device
    version moved since the plan was prepared, the device rejects the
    whole batch with VersionConflictError and nothing is written; do
    not retry the same plan -- prepare a new one from a fresh read.
    """
    if not isinstance(plan, WritePlan):
        raise ConfigError(
            f"plan must be a WritePlan, got {type(plan).__name__}"
        )
    if plan._consumed:
        raise ConfigError(
            "write plan already consumed; prepare a fresh plan from a "
            "fresh read instead of retrying a stale plan"
        )
    plan._consumed = True
    commit = getattr(device, "compare_version_and_write", None)
    if not callable(commit):
        raise ConfigError(
            f"device must provide compare_version_and_write, "
            f"got {type(device).__name__}"
        )
    return commit(
        plan.version_address,
        plan.version,
        tuple((w.address, w.new) for w in plan.writes),
    )
