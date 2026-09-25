"""Inspectable read planning for versioned register groups.

Given one :class:`~regmirror.scanner.RegisterGroup` the planner decides which
consecutive-register data reads ("blocks") a scan attempt should issue,
under three caller-supplied constraints:

* ``max_read_words`` -- no block may read more than this many registers.
* ``max_gap_words`` -- a block may span at most this many *extra*
  registers (registers no field occupies); previously the whole span
  between fields was read unconditionally, now a gap is bridged only
  when it merges neighbouring reads and the caller permits it.
* ``forbidden_ranges`` -- half-open ``[start, end)`` intervals that no
  block (and not the version read) may touch; a plan is impossible if a
  field or the version register itself lies inside one.

The version register is *never* part of the data plan: every attempt
still issues two dedicated singleton version reads, one before and one
after the data blocks, and the version check cannot be merged into a
data read.  The version address may still lie inside a block as an
ignored gap word (callers can forbid it explicitly).

Optimality key (applied per group; groups are never merged):

1. minimum number of **data** reads;
2. then minimum total extra registers read
   (``sum(block counts) - distinct covered registers``);
3. then lexicographically smallest tuple of ascending block starts.

Fields may overlap (when the group was built with ``allow_overlap=True``);
what must be covered is the *union* of every register the fields occupy,
so overlapping fields never cost duplicate reads.

The implementation is an O(m) sliding-window-minimum dynamic program over
the sorted covered registers (``m`` = number of distinct registers the
fields occupy).  ``tests/test_planner.py`` independently enumerates every
legal block combination on small address spaces (including slack and
overlapping blocks) to verify the DP's optimum.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import AddressError, ConfigError

__all__ = [
    "ReadBlock",
    "GroupPlan",
    "ScanPlan",
    "normalize_forbidden_ranges",
    "plan_group",
    "plan_scan",
]

MAX_WORD = 0xFFFF


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


def normalize_forbidden_ranges(
    ranges: object,
) -> tuple[tuple[int, int], ...]:
    """Validate half-open ``[start, end)`` ranges and merge overlaps.

    Merging is a canonicalisation, not a semantic change: the union of
    forbidden registers stays exactly the caller's union.  Adjacent
    ranges (``[a, b)`` + ``[b, c)``) are merged as well.
    """
    if isinstance(ranges, (str, bytes)) or not isinstance(ranges, Sequence):
        raise ConfigError(
            "forbidden_ranges must be a sequence of (start, end) pairs"
        )
    parsed: list[tuple[int, int]] = []
    for i, item in enumerate(ranges):
        if (
            isinstance(item, (str, bytes))
            or not isinstance(item, Sequence)
            or len(item) != 2
        ):
            raise ConfigError(
                f"forbidden_ranges[{i}] must be a (start, end) pair"
            )
        start = _check_int(f"forbidden_ranges[{i}].start", item[0])
        end = _check_int(f"forbidden_ranges[{i}].end", item[1])
        if not 0 <= start < end <= MAX_WORD + 1:
            raise ConfigError(
                f"forbidden_ranges[{i}] = ({start}, {end}) must satisfy "
                "0 <= start < end <= 65536"
            )
        parsed.append((start, end))
    parsed.sort()
    merged: list[tuple[int, int]] = []
    for start, end in parsed:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass(frozen=True)
class ReadBlock:
    """One consecutive data read ``read(start, count)``."""

    start: int
    count: int

    @property
    def end(self) -> int:
        """Inclusive last register of the block."""
        return self.start + self.count - 1

    def as_tuple(self) -> tuple[int, int]:
        return (self.start, self.count)


@dataclass(frozen=True)
class GroupPlan:
    """The fixed read plan for one group.

    Attributes:
        name: group name.
        version_address: the register read by the two version reads.
        blocks: ascending, pairwise disjoint data reads.
        covered_words: number of distinct registers the fields occupy.
    """

    name: str
    version_address: int
    blocks: tuple[ReadBlock, ...]
    covered_words: int

    @property
    def data_read_count(self) -> int:
        """Number of data reads in one attempt (optimisation target 1)."""
        return len(self.blocks)

    @property
    def total_data_words(self) -> int:
        return sum(b.count for b in self.blocks)

    @property
    def extra_words(self) -> int:
        """Registers read that no field occupies (optimisation target 2)."""
        return self.total_data_words - self.covered_words

    @property
    def attempt_reads(self) -> int:
        """Total device reads per attempt: version + data blocks + version."""
        return self.data_read_count + 2

    @property
    def block_starts(self) -> tuple[int, ...]:
        return tuple(b.start for b in self.blocks)


@dataclass(frozen=True)
class ScanPlan:
    """Plans of every group of one scan, in scan order."""

    groups: tuple[GroupPlan, ...]

    def __getitem__(self, group_name: str) -> GroupPlan:
        for p in self.groups:
            if p.name == group_name:
                return p
        raise KeyError(group_name)

    def __len__(self) -> int:
        return len(self.groups)


def _covered_registers(group: object) -> list[int]:
    covered: set[int] = set()
    for f in getattr(group, "fields"):
        covered.update(range(f.address, f.last_address + 1))
    return sorted(covered)


def _address_inside(address: int, ranges: Sequence[tuple[int, int]]) -> bool:
    if not ranges:
        return False
    starts = [r[0] for r in ranges]
    i = bisect_right(starts, address) - 1
    return i >= 0 and address < ranges[i][1]


def _check_possibility(
    group: object,
    covered: Sequence[int],
    *,
    size: int,
    forbidden: Sequence[tuple[int, int]],
) -> None:
    """Impossibility-by-construction checks; never touches the device."""
    name = getattr(group, "name")
    for a in covered:
        if a >= size:
            raise AddressError(
                f"group {name!r}: field register {a} outside device "
                f"image of size {size}"
            )
        if _address_inside(a, forbidden):
            raise ConfigError(
                f"group {name!r}: field register {a} lies in a forbidden "
                "read range; the field cannot be covered"
            )
    version = group.version_address
    if version >= size:
        raise AddressError(
            f"group {name!r}: version register {version} outside device "
            f"image of size {size}"
        )
    if _address_inside(version, forbidden):
        raise ConfigError(
            f"group {name!r}: version register {version} lies in a "
            "forbidden read range; the mandatory version read is impossible"
        )


def plan_group(
    group: object,
    *,
    size: int,
    max_read_words: int,
    max_gap_words: int,
    forbidden_ranges: Sequence[tuple[int, int]] | None = None,
) -> GroupPlan:
    """Compute the optimal :class:`GroupPlan` for one group.

    ``forbidden_ranges`` may be raw pairs or the output of
    :func:`normalize_forbidden_ranges`; when ``None`` the planner uses
    the group's own ``forbidden_ranges`` attribute (empty for a plain
    duck-typed group).  ``group`` only needs ``name``,
    ``version_address`` and fields with ``address`` / ``last_address``,
    which keeps the planner independent of the scanner module.
    """
    max_read_words = _check_int("max_read_words", max_read_words)
    if not 1 <= max_read_words <= MAX_WORD + 1:
        raise ConfigError(
            f"max_read_words must be in 1..65536, got {max_read_words}"
        )
    max_gap_words = _check_int("max_gap_words", max_gap_words)
    if not 0 <= max_gap_words <= MAX_WORD:
        raise ConfigError(
            f"max_gap_words must be in 0..65535, got {max_gap_words}"
        )
    size = _check_int("size", size)
    if not 1 <= size <= MAX_WORD + 1:
        raise ConfigError(f"size must be in 1..65536, got {size}")
    if forbidden_ranges is None:
        forbidden_ranges = getattr(group, "forbidden_ranges", ())
    forbidden = normalize_forbidden_ranges(forbidden_ranges)

    covered = _covered_registers(group)
    _check_possibility(
        group, covered, size=size, forbidden=forbidden
    )
    m = len(covered)
    # c[k] = address of the k-th distinct covered register.
    c = covered
    # d[k] = c[k] - k = number of non-covered registers before c[k];
    # nondecreasing in k.
    d = [c[k] - k for k in range(m)]
    starts = [r[0] for r in forbidden]

    # DP over covered-register prefix lengths.  One last block covering
    # c[j..i-1] is legal iff all of the following hold; every lower bound
    # on j is monotone in i, which is what makes the sliding minimum work:
    #
    #   span : c[i-1] - c[j] + 1 <= max_read_words
    #   gap  : (c[i-1] - c[j] + 1) - (i - j) <= max_gap_words
    #   hole : [c[j], c[i-1]] contains no forbidden register
    blocks = [0] * (m + 1)
    extra = [0] * (m + 1)
    first: list[tuple[int, ...]] = [()] * (m + 1)
    back = [0] * (m + 1)

    def key(j: int) -> tuple[int, int, tuple[int, ...]]:
        # Endpoint-independent part of the comparison between candidate
        # slice starts j: block count, extra-word contribution, and the
        # ascending block-start tuple (the final block starts at c[j]).
        return (blocks[j], extra[j] + j - c[j], first[j] + (c[j],))

    window: deque[int] = deque([0])

    def push(j: int) -> None:
        kj = key(j)
        while window and key(window[-1]) >= kj:
            window.pop()
        window.append(j)

    for i in range(1, m + 1):
        end_addr = c[i - 1]
        lo_span = bisect_left(c, end_addr - max_read_words + 1, 0, i)
        # gap: d[j] <= end_addr - i + 1 - max_gap_words
        lo_gap = bisect_left(
            d, end_addr - i + 1 - max_gap_words, 0, i
        )
        lo_hole = 0
        k = bisect_right(starts, end_addr) - 1
        if k >= 0:
            lo_hole = bisect_left(c, forbidden[k][1], 0, i)
        lo = max(lo_span, lo_gap, lo_hole)
        while window and window[0] < lo:
            window.popleft()
        if not window:
            raise ConfigError(
                f"group {getattr(group, 'name', '?')!r}: registers "
                f"around {end_addr} cannot be covered by any read of at "
                f"most {max_read_words} words; max_read_words is smaller "
                "than a field span or forbidden ranges split the field"
            )
        j = window[0]
        back[i] = j
        blocks[i] = blocks[j] + 1
        extra[i] = extra[j] + (end_addr + 1 - i) + j - c[j]
        first[i] = first[j] + (c[j],)
        # i is a legal slice start for later endpoints only while it
        # points at an actual covered register (i < m).
        if i < m:
            push(i)

    built: list[ReadBlock] = []
    i = m
    while i > 0:
        j = back[i]
        built.append(ReadBlock(c[j], c[i - 1] - c[j] + 1))
        i = j
    built.reverse()

    plan = GroupPlan(
        name=group.name,
        version_address=group.version_address,
        blocks=tuple(built),
        covered_words=m,
    )
    if plan.block_starts != tuple(first[m]):  # pragma: no cover - invariant
        raise AssertionError("planner start tuple mismatch")
    if plan.extra_words != extra[m]:  # pragma: no cover - invariant
        raise AssertionError("planner extra-word mismatch")
    return plan


def plan_scan(
    groups: Sequence[object],
    *,
    size: int,
    max_read_words: int,
    max_gap_words: int,
    forbidden_ranges: Sequence[tuple[int, int]] = (),
) -> ScanPlan:
    """Plan every group independently; groups are never merged.

    The shared ``forbidden_ranges`` apply to *every* group, in addition
    to each group's own ``forbidden_ranges``.
    """
    shared = normalize_forbidden_ranges(forbidden_ranges)
    plans: list[GroupPlan] = []
    for g in groups:
        own = tuple(getattr(g, "forbidden_ranges", ()))
        plans.append(
            plan_group(
                g,
                size=size,
                max_read_words=max_read_words,
                max_gap_words=max_gap_words,
                forbidden_ranges=tuple(shared) + tuple(own),
            )
        )
    return ScanPlan(tuple(plans))
