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
* ``commit(*version_addresses)`` -- publish the register state written by
                                    earlier changes in the same script
                                    step as one immutable commit
                                    (:class:`HistoryDevice` only).

The script is a list of steps; step *k* is applied after the *k*-th
``read`` call and before the *(k+1)*-th.  A step may be a single
``Change`` or a tuple of ``Change`` objects applied together.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import AddressError, DeviceReadError, SnapshotUnavailableError

__all__ = ["Change", "DeviceMirror", "HistoryDevice"]

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

    @classmethod
    def commit(cls, *version_addresses: int) -> "Change":
        """Publish the current image state as one immutable commit.

        ``version_addresses`` are the 16-bit version registers guarded
        by this commit; each is bumped by 2 mod 2**16 (even values only)
        as part of the same atomic publish.  Only meaningful on
        :class:`HistoryDevice`; a plain :class:`DeviceMirror` rejects
        commit changes in its script.
        """
        if not version_addresses:
            raise ValueError("commit requires at least one version address")
        addrs = tuple(_check_int("version_address", a) for a in version_addresses)
        if len(set(addrs)) != len(addrs):
            raise ValueError("commit version addresses must be unique")
        return cls("commit", -1, addrs)


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
            self._script.append(self._normalize_step(i, step))
        self._reads = 0

    def _normalize_step(self, i: int, step: object) -> tuple[Change, ...]:
        if isinstance(step, Change):
            changes = (step,)
        elif isinstance(step, tuple) and all(
            isinstance(c, Change) for c in step
        ):
            # A tuple applies its changes together; () is a no-op
            # step, useful for aligning the schedule with reads.
            changes = step
        else:
            raise TypeError(
                f"script step {i} must be a Change or a "
                f"tuple of Change, got {type(step).__name__}"
            )
        for c in changes:
            if c.kind == "commit":
                raise TypeError(
                    f"script step {i}: Change.commit requires a "
                    "HistoryDevice, not a plain DeviceMirror"
                )
        return changes

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
        if change.kind == "commit":  # pragma: no cover - guarded at build time
            raise TypeError("Change.commit requires a HistoryDevice")
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


class HistoryDevice(DeviceMirror):
    """A scripted device that keeps an immutable, bounded commit history.

    Every ``Change.commit(...)`` in a script step (or an explicit
    :meth:`commit_now`) publishes the current image as one immutable
    commit with a monotonically increasing, gap-free sequence number.
    Historical reads :meth:`read_at` return words from one frozen commit,
    so updates committed while a historical read is in flight can never
    mix into its fields.

    Parameters:
        size, script, initial: as in :class:`DeviceMirror`.
        history_depth: retain at most this many commits.  ``None`` (the
                       default) keeps every commit.  Pruning always drops
                       the oldest retained commit, but never a commit that
                       is currently pinned by a historical reader.
        initial_committed: when True (default) the initial image is
                       published as sequence 0 before the script runs.
                       When False the device starts with no history.

    Sequence semantics: sequences start at 0 and are dense through
    :attr:`latest_sequence` (the initial image, when published, is
    seq 0; otherwise the first ``commit_now`` is seq 0).  A request for
    a sequence that existed but was pruned raises
    :class:`SnapshotUnavailableError` with ``reason="history_pruned"``;
    one that never existed raises it with
    ``reason="history_insufficient"``.
    """

    def __init__(
        self,
        size: int,
        script: Sequence[object] = (),
        initial: dict[int, int] | None = None,
        *,
        history_depth: int | None = None,
        initial_committed: bool = True,
    ):
        if not isinstance(initial_committed, bool):
            raise TypeError("initial_committed must be a bool")
        if history_depth is not None:
            history_depth = _check_int("history_depth", history_depth)
            if history_depth < 1:
                raise ValueError("history_depth must be >= 1 or None")
        self._depth = history_depth
        super().__init__(size, script=script, initial=initial)
        self._seqs: list[int] = []
        self._images: dict[int, tuple[int, ...]] = {}
        self._pins: dict[int, int] = {}
        self._versions: dict[int, int] = {}
        self._first_ever: int | None = None
        self._next_seq = 0
        if initial_committed:
            self._publish(())

    # -- script handling -------------------------------------------------

    def _normalize_step(self, i: int, step: object) -> tuple[Change, ...]:
        if isinstance(step, Change):
            changes = (step,)
        elif isinstance(step, tuple) and all(
            isinstance(c, Change) for c in step
        ):
            changes = step
        else:
            raise TypeError(
                f"script step {i} must be a Change or a "
                f"tuple of Change, got {type(step).__name__}"
            )
        for c in changes:
            if c.kind == "commit":
                for a in c.words:
                    if not 0 <= a < self.size:
                        raise AddressError(
                            f"script step {i}: commit version register "
                            f"{a} outside image of size {self.size}"
                        )
        return changes

    def _apply(self, change: Change) -> None:
        if change.kind == "commit":
            self.commit_now(*change.words)
        else:
            super()._apply(change)

    # -- manual driving (in addition to scripts) ------------------------

    def stage(self, change: Change) -> None:
        """Apply a non-commit change to the live working image.

        The change becomes visible to ordinary :meth:`read` calls only;
        it enters history at the next :meth:`commit_now`.
        """
        if not isinstance(change, Change):
            raise TypeError(
                f"change must be a Change, got {type(change).__name__}"
            )
        if change.kind == "commit":
            raise TypeError("use commit_now() to publish a commit")
        self._apply(change)

    def commit_now(self, *version_addresses: int) -> int:
        """Publish the live image as one commit; return its sequence.

        Each named version register is bumped to the next even value
        (``+2 mod 2**16``) as part of the atomic publish, regardless of
        the odd "write in progress" word it held while being staged.
        """
        if not version_addresses:
            raise ValueError("commit_now requires at least one version address")
        addrs = tuple(_check_int("version_address", a) for a in version_addresses)
        if len(set(addrs)) != len(addrs):
            raise ValueError("commit version addresses must be unique")
        return self._publish(addrs)

    def _publish(self, addrs: tuple[int, ...]) -> int:
        for a in addrs:
            if not 0 <= a < len(self._regs):
                raise AddressError(
                    f"commit version register {a} outside image of "
                    f"size {len(self._regs)}"
                )
            base = self._versions.get(a)
            if base is None:
                word = self._regs[a]
                base = word if not word & 1 else 0
            new = (base + 2) & 0xFFFF
            self._regs[a] = new
            self._versions[a] = new
        seq = self._next_seq
        self._next_seq += 1
        if self._first_ever is None:
            self._first_ever = seq
        self._seqs.append(seq)
        self._images[seq] = tuple(self._regs)
        self._prune()
        return seq

    def _prune(self) -> None:
        if self._depth is None:
            return
        while len(self._seqs) > self._depth:
            oldest = self._seqs[0]
            # A pinned commit is in use by a reader; the retention window
            # is temporarily extended rather than evicting it.
            if oldest in self._pins:
                return
            self._seqs.pop(0)
            del self._images[oldest]

    # -- history introspection ------------------------------------------

    @property
    def first_sequence(self) -> int | None:
        """Smallest sequence number ever assigned, or None if no commits."""
        return self._first_ever

    @property
    def oldest_sequence(self) -> int | None:
        """Oldest sequence still retained, or None without any history."""
        return self._seqs[0] if self._seqs else None

    @property
    def latest_sequence(self) -> int | None:
        """Newest committed sequence, or None without any history."""
        return self._seqs[-1] if self._seqs else None

    @property
    def retained_sequences(self) -> tuple[int, ...]:
        return tuple(self._seqs)

    def retains(self, seq: int) -> bool:
        """Whether commit ``seq`` is currently available to read."""
        return seq in self._images

    def resolve_barrier(self, barrier: int) -> int:
        """Return the last retained sequence <= barrier.

        Raises :class:`SnapshotUnavailableError` when no such commit
        exists (never existed, or already pruned).
        """
        if not self._seqs:
            raise SnapshotUnavailableError(
                None, barrier, "history_insufficient", None
            )
        oldest = self._seqs[0]
        if barrier < oldest:
            if self._first_ever is not None and barrier >= self._first_ever:
                reason = "history_pruned"
            else:
                reason = "history_insufficient"
            raise SnapshotUnavailableError(
                None, barrier, reason, oldest
            )
        i = bisect.bisect_right(self._seqs, barrier) - 1
        return self._seqs[i]

    # -- pinned historical reads ----------------------------------------

    def pin(self, seq: int) -> None:
        """Pin commit ``seq`` so pruning cannot evict it while reading."""
        if seq not in self._images:
            oldest = self._seqs[0] if self._seqs else None
            if self._first_ever is not None and self._first_ever <= seq <= (
                self._next_seq - 1
            ):
                reason = "history_pruned"
            else:
                reason = "history_insufficient"
            raise SnapshotUnavailableError(None, seq, reason, oldest)
        self._pins[seq] = self._pins.get(seq, 0) + 1

    def unpin(self, seq: int) -> None:
        """Release a pin acquired with :meth:`pin`."""
        n = self._pins.get(seq, 0)
        if n <= 1:
            self._pins.pop(seq, None)
        else:
            self._pins[seq] = n - 1
        self._prune()

    def read_at(
        self, seq: int, start: int, count: int
    ) -> tuple[int, ...]:
        """Read from the frozen image of commit ``seq``.

        Unlike :meth:`read`, this never advances the change script and is
        unaffected by commits made afterwards; the caller is expected to
        have :meth:`pin`-ed ``seq`` for the read's duration.
        """
        image = self._images.get(seq)
        if image is None:
            oldest = self._seqs[0] if self._seqs else None
            if self._first_ever is not None and self._first_ever <= seq <= (
                self._next_seq - 1
            ):
                reason = "history_pruned"
            else:
                reason = "history_insufficient"
            raise SnapshotUnavailableError(None, seq, reason, oldest)
        start = _check_int("start", start)
        count = _check_int("count", count)
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        if start < 0 or start + count > len(image):
            raise AddressError(
                f"read_at({seq}, {start}, {count}) outside image of size "
                f"{len(image)}"
            )
        return tuple(image[start : start + count])
