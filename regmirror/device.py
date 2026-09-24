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

import collections
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import AddressError, DeviceReadError

__all__ = [
    "Change",
    "DeviceMirror",
    "CommitRecord",
    "CommitStore",
    "HistoricalDevice",
]

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


# --------------------------------------------------------------------------
# Versioned commit history (barrier-scan iteration)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitRecord:
    """One immutable, stable point in a device's history.

    Attributes:
        seq:     global logical commit sequence number; commits inside one
                 store are strictly increasing, but need not be contiguous
                 (different devices commit at different global seqs).
        version: the even 16-bit version word of this stable state.
        words:   a full, private copy of every register word at this commit.
                 Records never change after creation, even if the device
                 keeps mutating or the bounded store drops the record.
    """

    seq: int
    version: int
    words: tuple[int, ...]


class CommitStore:
    """Append-only, optionally bounded history of :class:`CommitRecord`.

    ``max_records=None`` keeps every commit; a finite bound drops the
    oldest record(s) when exceeded, modelling a device with finite
    history storage.  Dropping a record does not mutate any
    :class:`CommitRecord` previously handed out.
    """

    def __init__(self, max_records: int | None = None):
        if isinstance(max_records, bool) or not isinstance(max_records, int):
            if max_records is not None:
                raise TypeError(
                    "max_records must be an int or None, got "
                    f"{type(max_records).__name__}"
                )
        elif max_records < 1:
            raise ValueError(f"max_records must be >= 1, got {max_records}")
        self._max = max_records
        self._records: collections.deque[CommitRecord] = collections.deque()

    def __len__(self) -> int:
        return len(self._records)

    @property
    def max_records(self) -> int | None:
        return self._max

    @property
    def oldest(self) -> CommitRecord | None:
        return self._records[0] if self._records else None

    @property
    def newest(self) -> CommitRecord | None:
        return self._records[-1] if self._records else None

    @property
    def oldest_seq(self) -> int | None:
        rec = self.oldest
        return None if rec is None else rec.seq

    def append(self, seq: int, version: int, words: Sequence[int]) -> CommitRecord:
        """Record a new stable commit; ``seq`` must strictly increase."""
        seq = _check_int("seq", seq)
        version = _check_word("version", version)
        if version & 1:
            raise ValueError(
                f"committed version must be even (stable), got {version}"
            )
        words = tuple(_check_word(f"words[{i}]", w) for i, w in enumerate(words))
        newest = self.newest
        if newest is not None and seq <= newest.seq:
            raise ValueError(
                f"commit seq {seq} must be greater than newest seq {newest.seq}"
            )
        record = CommitRecord(seq, version, words)
        self._records.append(record)
        if self._max is not None:
            while len(self._records) > self._max:
                self._records.popleft()
        return record

    def latest_at_or_before(self, barrier: int) -> CommitRecord | None:
        """Return the last commit with ``seq <= barrier``, or ``None``.

        ``None`` means the retained history does not reach the barrier
        (the barrier predates the oldest retained commit).
        """
        barrier = _check_int("barrier", barrier)
        for rec in reversed(self._records):
            if rec.seq <= barrier:
                return rec
        return None

    def records(self) -> tuple[CommitRecord, ...]:
        """All retained records, oldest first (snapshot for inspection)."""
        return tuple(self._records)


@dataclass(frozen=True)
class DeviceCommit:
    """One scripted step that atomically commits a new stable state.

    Use :meth:`at` in a :class:`HistoricalDevice` script.  The step
    applies ``changes`` (data only), sets the version register to the
    even ``version`` word, and appends a full-image :class:`CommitRecord`
    with global sequence number ``seq`` (or the device's next seq).
    """

    seq: int | None
    version: int
    changes: tuple[Change, ...]

    @classmethod
    def at(
        cls,
        version: int,
        changes: Sequence[Change] = (),
        seq: int | None = None,
    ) -> "DeviceCommit":
        version = _check_word("version", version)
        if version & 1:
            raise ValueError(
                f"committed version must be even (stable), got {version}"
            )
        changes = tuple(changes)
        for i, c in enumerate(changes):
            if not isinstance(c, Change):
                raise TypeError(
                    f"changes[{i}] must be a Change, got {type(c).__name__}"
                )
        if seq is not None:
            seq = _check_int("seq", seq)
        return cls(seq, version, changes)


class HistoricalDevice(DeviceMirror):
    """A scripted :class:`DeviceMirror` that keeps versioned commit history.

    Parameters:
        device_id: stable non-empty name used by mirror requests.
        size:      image size in 16-bit words.
        version_address: address of the device's 16-bit version register.
        initial_version: even version word of the initial stable state.
        initial:   initial data words (must not contain ``version_address``;
                   use ``initial_version`` for the version word).
        start_seq: global seq assigned to the initial commit (>= 1).
        max_records: bounded history retention (``None`` = unbounded).
        script:    steps applied between reads; each step is a
                   :class:`Change`, a tuple of ``Change``, or a
                   :class:`DeviceCommit` (which also records history).
        faults:    ``{read_ordinal: DeviceReadError}``; the k-th ``read``
                   (0-based) raises the faulted error instead of serving.
                   A faulted read still consumes its ordinal; no script
                   step is applied for it.

    An initial :class:`CommitRecord` is appended at construction, so a
    freshly built device already has one stable state in its history.
    """

    def __init__(
        self,
        device_id: str,
        size: int,
        version_address: int,
        *,
        initial_version: int = 0,
        initial: dict[int, int] | None = None,
        start_seq: int = 1,
        max_records: int | None = None,
        script: Sequence[object] = (),
        faults: Mapping[int, DeviceReadError] | None = None,
    ):
        if not isinstance(device_id, str) or not device_id:
            raise TypeError("device_id must be a non-empty str")
        version_address = _check_int("version_address", version_address)
        if not 0 <= version_address <= MAX_ADDRESS:
            raise ValueError(
                f"version_address {version_address} outside 0..65535"
            )
        initial_version = _check_word("initial_version", initial_version)
        if initial_version & 1:
            raise ValueError(
                "initial_version must be even (stable), "
                f"got {initial_version}"
            )
        start_seq = _check_int("start_seq", start_seq)
        if start_seq < 1:
            raise ValueError(f"start_seq must be >= 1, got {start_seq}")
        if initial and version_address in initial:
            raise ValueError(
                "initial must not contain the version register "
                f"{version_address}; use initial_version"
            )
        super().__init__(size, script=(), initial=initial)
        if version_address >= size:
            raise ValueError(
                f"version_address {version_address} outside image of size {size}"
            )
        self._device_id = device_id
        self._version_address = version_address
        self._regs[version_address] = initial_version
        self._next_seq = start_seq
        self.history = CommitStore(max_records)
        self.history.append(start_seq, initial_version, tuple(self._regs))
        self._next_seq = start_seq + 1
        steps: list[object] = []
        for i, step in enumerate(script):
            if isinstance(step, (Change, DeviceCommit)):
                steps.append(step)
            elif isinstance(step, tuple) and all(
                isinstance(c, Change) for c in step
            ):
                steps.append(step)
            else:
                raise TypeError(
                    f"script step {i} must be a Change, a tuple of Change, "
                    f"or a DeviceCommit, got {type(step).__name__}"
                )
        self._script = steps
        self._faults: dict[int, DeviceReadError] = {}
        if faults:
            for ordinal, fault in faults.items():
                ordinal = _check_int("fault read ordinal", ordinal)
                if ordinal < 0:
                    raise ValueError("fault read ordinal must be >= 0")
                if not isinstance(fault, DeviceReadError):
                    raise TypeError(
                        "fault values must be DeviceReadError, got "
                        f"{type(fault).__name__}"
                    )
                self._faults[ordinal] = fault

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def version_address(self) -> int:
        return self._version_address

    def read(self, start: int, count: int) -> tuple[int, ...]:
        # Validate the request the same way the parent does so a faulted
        # call and a served call share bounds/type semantics.
        start = _check_int("start", start)
        count = _check_int("count", count)
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        if start < 0 or start + count > len(self._regs):
            raise AddressError(
                f"read({start}, {count}) outside image of size {len(self._regs)}"
            )
        ordinal = self._reads
        fault = self._faults.get(ordinal)
        words = tuple(self._regs[start : start + count])
        self._apply_step(ordinal)
        self._reads += 1
        if fault is not None:
            raise fault
        return words

    def _apply_step(self, index: int) -> None:
        if index >= len(self._script):
            return
        step = self._script[index]
        if isinstance(step, DeviceCommit):
            self._apply_commit(step)
        else:
            changes = (step,) if isinstance(step, Change) else step
            for change in changes:
                self._apply(change)

    def _apply_commit(self, commit: DeviceCommit) -> None:
        # Validate every change before applying any of them, so a bad
        # commit never leaves the image half-written.
        plans: list[tuple[int, tuple[int, ...]]] = []
        for change in commit.changes:
            words = self._change_words(change)
            end = change.address + len(words)
            if change.address < 0 or end > len(self._regs):
                raise ValueError(
                    f"commit write at {change.address}..{end - 1} outside "
                    f"image of size {len(self._regs)}"
                )
            if change.address <= self._version_address < end:
                raise ValueError(
                    "commit data changes must not write the version register "
                    f"{self._version_address}"
                )
            plans.append((change.address, words))
        for address, words in plans:
            self._regs[address : address + len(words)] = words
        self._regs[self._version_address] = commit.version
        seq = commit.seq if commit.seq is not None else self._next_seq
        if seq < self._next_seq:
            raise ValueError(
                f"commit seq {seq} must be >= next seq {self._next_seq}"
            )
        self.history.append(seq, commit.version, tuple(self._regs))
        self._next_seq = seq + 1

    @staticmethod
    def _change_words(change: Change) -> tuple[int, ...]:
        if change.kind == "field":
            from .fields import encode_field

            return encode_field(change.field, change.value)
        return change.words
