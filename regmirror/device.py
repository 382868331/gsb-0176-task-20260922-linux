"""Deterministic in-memory device image with a scripted change schedule.

``DeviceMirror`` exposes the single operation the scanner relies on::

    read(start, count) -> tuple of ``count`` 16-bit words

Between consecutive ``read`` calls the image applies the next scripted
change (if any), which models a device that keeps writing registers while
a scan is in flight.  All changes are driven by a fixed, deterministic
script so tests and the demo are fully reproducible offline.

Script vocabulary (see ``Change``):

* ``set_words(addr, words)``     -- write raw 16-bit words.
* ``set_version(addr, value)``   -- write the 16-bit version register
                                    (odd = write in progress, even = stable).
* ``write_field(field, value)``  -- encode ``value`` per the field's
                                    word/byte order and write the words.

The script is a list of steps; step *k* is applied after the *k*-th
``read`` call and before the *(k+1)*-th.  A step may be a single
``Change`` or a tuple of ``Change`` objects applied together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import (
    AddressError,
    DeviceReadError,
    PlanConflictError,
    VersionConflictError,
)

__all__ = ["Change", "DeviceMirror", "CompareWriteResult"]

MAX_ADDRESS = 0xFFFF  # 16-bit register address space


def _check_int(name: str, value: object) -> int:
    # bool is a subclass of int; reject it explicitly per the public contract.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


def _check_word(name: str, value: object) -> int:
    value = _check_int(name, value)
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must be a 16-bit word (0..65535), got {value}")
    return value


@dataclass(frozen=True)
class Change:
    """One scripted mutation of the image.

    Exactly one of the three factory classmethods should be used.
    """

    kind: str
    address: int
    words: tuple[int, ...] = ()
    field: object = None  # FieldSpec, only for kind == "field"
    value: object = None  # raw integer value, only for kind == "field"

    @classmethod
    def set_words(cls, address: int, words: Sequence[int]) -> "Change":
        address = _check_int("address", address)
        checked = tuple(_check_word(f"words[{i}]", w) for i, w in enumerate(words))
        if not checked:
            raise ValueError("set_words requires at least one word")
        return cls("words", address, checked)

    @classmethod
    def set_version(cls, address: int, value: int) -> "Change":
        address = _check_int("address", address)
        value = _check_word("version", value)
        return cls("version", address, (value,))

    @classmethod
    def write_field(cls, field: object, value: int) -> "Change":
        from .fields import FieldSpec  # local import: fields imports nothing here

        if not isinstance(field, FieldSpec):
            raise TypeError(
                f"field must be a FieldSpec, got {type(field).__name__}"
            )
        value = _check_int("value", value)
        return cls("field", field.address, (), field, value)


class DeviceMirror:
    """A 16-bit register image that mutates according to a fixed script.

    Parameters:
        size:   number of 16-bit registers (1..65536).
        script: sequence of steps; step *k* is applied after the *k*-th
                ``read`` call.  Each step is a ``Change`` or a tuple of
                ``Change`` objects.  Steps beyond the list are no-ops.
        initial: optional mapping {address: word} applied before any read.

    The image validates every ``read`` and raises :class:`AddressError`
    for out-of-range requests, mimicking a well-behaved device.
    """

    def __init__(
        self,
        size: int,
        script: Sequence[object] = (),
        initial: dict[int, int] | None = None,
    ):
        size = _check_int("size", size)
        if not 1 <= size <= MAX_ADDRESS + 1:
            raise ValueError(f"size must be in 1..65536, got {size}")
        self._regs = [0] * size
        if initial:
            for addr, word in initial.items():
                addr = _check_int("initial address", addr)
                word = _check_word("initial word", word)
                if addr >= size:
                    raise AddressError(
                        f"initial address {addr} outside image of size {size}"
                    )
                self._regs[addr] = word
        self._script: list[tuple[Change, ...]] = []
        for i, step in enumerate(script):
            if isinstance(step, Change):
                self._script.append((step,))
            elif isinstance(step, tuple) and all(
                isinstance(c, Change) for c in step
            ):
                # A tuple applies its changes together; () is a no-op
                # step, useful for aligning the schedule with reads.
                self._script.append(step)
            else:
                raise TypeError(
                    f"script step {i} must be a Change or a "
                    f"tuple of Change, got {type(step).__name__}"
                )
        self._reads = 0

    @property
    def size(self) -> int:
        return len(self._regs)

    @property
    def read_count(self) -> int:
        """How many ``read`` calls have been served so far."""
        return self._reads

    def read(self, start: int, count: int) -> tuple[int, ...]:
        """Read ``count`` consecutive 16-bit registers from ``start``."""
        start = _check_int("start", start)
        count = _check_int("count", count)
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        if start < 0 or start + count > len(self._regs):
            raise AddressError(
                f"read({start}, {count}) outside image of size {len(self._regs)}"
            )
        words = tuple(self._regs[start : start + count])
        self._apply_step(self._reads)
        self._reads += 1
        return words

    def _apply_step(self, index: int) -> None:
        if index >= len(self._script):
            return
        for change in self._script[index]:
            self._apply(change)

    def _apply(self, change: Change) -> None:
        if change.kind == "field":
            from .fields import encode_field

            words = encode_field(change.field, change.value)
        else:
            words = change.words
        end = change.address + len(words)
        if change.address < 0 or end > len(self._regs):
            raise AddressError(
                f"scripted write at {change.address}..{end - 1} outside "
                f"image of size {len(self._regs)}"
            )
        self._regs[change.address : end] = words

    # Convenience for tests/demo: read without advancing the script.
    def peek(self, start: int, count: int) -> tuple[int, ...]:
        start = _check_int("start", start)
        count = _check_int("count", count)
        if start < 0 or start + count > len(self._regs):
            raise AddressError(
                f"peek({start}, {count}) outside image of size {len(self._regs)}"
            )
        return tuple(self._regs[start : start + count])

    def compare_version_and_write(
        self,
        version_address: int,
        expected_version: int,
        updates: dict[int, int],
        new_values: dict[int, int],
    ) -> "CompareWriteResult":
        """Apply one masked batch write iff the version still matches.

        This is the *simulated device side* of the local compare-and-write
        protocol -- an atomic state-machine transition, not a claim that a
        real device offers such a command.

        For every register address, ``word = (word & ~mask) | value`` where
        ``mask = updates[address]`` and ``value = new_values[address]``.
        All register changes and the version bump (+2 mod 2**16) happen in
        one indivisible step, or nothing happens at all.

        The batch is refused (no register touched, version unchanged) when
        the current version word differs from ``expected_version`` or is
        odd (a real device would be mid-commit).
        """
        version_address = _check_int("version_address", version_address)
        expected_version = _check_word("expected_version", expected_version)
        if not (0 <= version_address < len(self._regs)):
            raise AddressError(
                f"version_address {version_address} outside image of "
                f"size {len(self._regs)}"
            )
        if not isinstance(updates, dict) or not isinstance(new_values, dict):
            raise TypeError("updates and new_values must be dicts")
        if set(updates) != set(new_values):
            raise PlanConflictError(
                "updates and new_values must cover the same addresses"
            )
        if version_address in updates:
            raise PlanConflictError(
                f"the version register {version_address} cannot be part "
                "of a write batch"
            )
        merged: dict[int, int] = {}
        for addr in sorted(updates):
            a = _check_int("register address", addr)
            mask = _check_word("mask", updates[a])
            value = _check_word("new value", new_values[a])
            if mask == 0:
                raise PlanConflictError(
                    f"register {a}: mask must select at least one bit"
                )
            if value & ~mask:
                raise PlanConflictError(
                    f"register {a}: new value {value:#06x} sets bits "
                    f"outside mask {mask:#06x}"
                )
            if not (0 <= a < len(self._regs)):
                raise AddressError(
                    f"register {a} outside image of size {len(self._regs)}"
                )
            merged[a] = (mask, value)

        current = self._regs[version_address]
        if current != expected_version or (current & 1):
            raise VersionConflictError(expected_version, current)

        for addr, (mask, value) in merged.items():
            self._regs[addr] = (self._regs[addr] & ~mask) | value
        self._regs[version_address] = (current + 2) & 0xFFFF
        return CompareWriteResult(
            version_address=version_address,
            version_before=current,
            version_after=self._regs[version_address],
            masks=tuple((a, merged[a][0]) for a in sorted(merged)),
            old_words=tuple(
                (a, (self._regs[a] & merged[a][0]) | 0) for a in sorted(merged)
            ),
        )


@dataclass(frozen=True)
class CompareWriteResult:
    """Device-side acknowledgement of a committed compare-and-write."""

    version_address: int
    version_before: int
    version_after: int
    masks: tuple[tuple[int, int], ...]
    old_words: tuple[tuple[int, int], ...]


def check_read_result(start: int, count: int, result: object) -> tuple[int, ...]:
    """Validate what a foreign device object returned from ``read``.

    The scanner uses this on every device response so a misbehaving image
    fails loudly instead of silently corrupting the snapshot.
    """
    if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
        raise DeviceReadError(
            f"read({start}, {count}) returned {type(result).__name__}, "
            "expected a sequence of 16-bit words"
        )
    words = tuple(result)
    if len(words) != count:
        raise DeviceReadError(
            f"read({start}, {count}) returned {len(words)} words"
        )
    for i, w in enumerate(words):
        if isinstance(w, bool) or not isinstance(w, int):
            raise DeviceReadError(
                f"read({start}, {count}) word {i} is "
                f"{type(w).__name__}, expected int"
            )
        if not 0 <= w <= 0xFFFF:
            raise DeviceReadError(
                f"read({start}, {count}) word {i} = {w}, not a 16-bit word"
            )
    return words
