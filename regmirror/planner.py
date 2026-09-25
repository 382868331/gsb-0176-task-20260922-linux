"""Inspectable read planner: minimal block plans over a register image.

Given the registers a group actually occupies, the planner merges field
spans into as few ``read(start, count)`` blocks as possible.  The caller
supplies three limits:

* ``max_read_words`` -- upper bound on one block's length (device limit).
* ``max_gap_words``  -- how many *extra* registers a single gap inside a
                        block may span, i.e. how far apart two occupied
                        regions may be while still being read in one block.
* ``forbidden``      -- inclusive address ranges no block may touch at all.

Optimization contract (fixed, applied in this order):

1. **fewest blocks** (fewest data reads);
2. **fewest extra registers** -- ``sum(block lengths) - #required``,
   counting gap registers read (overlapping blocks would also count;
   the planner never emits them);
3. **lexicographically smallest tuple of block starts** -- a fully
   deterministic tie-break.  Note this prefers short blocks early:
   covering registers 0..2 with at most 2 words per block yields starts
   ``(0, 1)`` (blocks ``[0,0]`` and ``[1,2]``), not ``(0, 2)``.

A block is *legal* iff

* its length is in ``1..max_read_words``,
* it contains no forbidden register,
* every run of non-required registers inside it is at most
  ``max_gap_words`` long, and
* it starts and ends on required registers (a block never extends past
  the occupied span -- that would only add extra registers).

Plans are computed per group: blocks never span groups, and the version
register is always read on its own, never folded into a data block.

The planner is *inspectable*: :func:`plan_reads` returns a self-describing
:class:`ReadPlan`, and :func:`check_read_plan` independently verifies any
plan against the legality contract above (raising :class:`ConfigError`
with a locating message on the first violation).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .errors import ConfigError

__all__ = [
    "ReadBlock",
    "ReadPlan",
    "plan_reads",
    "check_read_plan",
    "normalize_intervals",
]

MAX_ADDRESS = 0xFFFF  # 16-bit register address space


def _check_int(name: str, value: object) -> int:
    # bool is a subclass of int; reject it explicitly per the public contract.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


def normalize_intervals(
    name: str, intervals: object
) -> tuple[tuple[int, int], ...]:
    """Validate and normalize inclusive (start, end) address intervals.

    Returns them sorted and merged (overlapping or adjacent intervals
    coalesce).  Every element must be a pair of ints in 0..65535 with
    start <= end; anything else raises :class:`ConfigError` locating the
    offending element.
    """
    if isinstance(intervals, (str, bytes)):
        raise ConfigError(
            f"{name} must be an iterable of (start, end) pairs, "
            f"got {intervals!r}"
        )
    try:
        iterator = iter(intervals)  # type: ignore[arg-type]
    except TypeError:
        raise ConfigError(
            f"{name} must be an iterable of (start, end) pairs, "
            f"got {type(intervals).__name__}"
        ) from None
    raw: list[tuple[int, int]] = []
    for i, item in enumerate(iterator):
        try:
            start, end = item
        except (TypeError, ValueError):
            raise ConfigError(
                f"{name}[{i}] must be a (start, end) pair, got {item!r}"
            ) from None
        start = _check_int(f"{name}[{i}] start", start)
        end = _check_int(f"{name}[{i}] end", end)
        if not 0 <= start <= MAX_ADDRESS or not 0 <= end <= MAX_ADDRESS:
            raise ConfigError(
                f"{name}[{i}] = ({start}, {end}) outside 0..{MAX_ADDRESS}"
            )
        if start > end:
            raise ConfigError(
                f"{name}[{i}] start {start} is after end {end}"
            )
        raw.append((start, end))
    raw.sort()
    merged: list[list[int]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return tuple((s, e) for s, e in merged)


@dataclass(frozen=True)
class ReadBlock:
    """One planned ``read(start, count)`` of consecutive 16-bit registers."""

    start: int
    count: int

    @property
    def last(self) -> int:
        """Inclusive last register address covered by this block."""
        return self.start + self.count - 1


@dataclass(frozen=True)
class ReadPlan:
    """An inspectable plan: the blocks one attempt reads for one group.

    Carries the normalized inputs alongside the blocks so the plan is
    self-describing.  This is a plain data container; legality is
    verified independently by :func:`check_read_plan` (or ``plan.check()``).
    """

    blocks: tuple[ReadBlock, ...]
    required: tuple[tuple[int, int], ...]  # merged required intervals
    forbidden: tuple[tuple[int, int], ...]  # merged forbidden intervals
    max_read_words: int
    max_gap_words: int

    @property
    def read_count(self) -> int:
        """Number of data reads one attempt performs (version reads excluded)."""
        return len(self.blocks)

    @property
    def total_registers(self) -> int:
        """Registers read per attempt, counting every block's length."""
        return sum(b.count for b in self.blocks)

    @property
    def required_registers(self) -> int:
        """Registers the caller actually asked to be covered."""
        return sum(e - s + 1 for s, e in self.required)

    @property
    def extra_registers(self) -> int:
        """Registers read beyond the required set (bridged gap registers)."""
        return self.total_registers - self.required_registers

    @property
    def starts(self) -> tuple[int, ...]:
        """Block start addresses, ascending -- the tie-break criterion."""
        return tuple(b.start for b in self.blocks)

    def check(self) -> None:
        """Verify this plan against its own contract; raise ConfigError."""
        check_read_plan(
            self,
            self.required,
            max_read_words=self.max_read_words,
            max_gap_words=self.max_gap_words,
            forbidden=self.forbidden,
        )


def _touches(
    forbidden: tuple[tuple[int, int], ...],
    forbidden_starts: list[int],
    lo: int,
    hi: int,
) -> bool:
    """True iff any merged forbidden interval intersects [lo, hi]."""
    i = bisect_right(forbidden_starts, hi) - 1
    return i >= 0 and forbidden[i][1] >= lo


def _clusters(
    required: tuple[tuple[int, int], ...],
    forbidden: tuple[tuple[int, int], ...],
    forbidden_starts: list[int],
    max_gap_words: int,
):
    """Yield sorted register lists, split at unbridgeable gaps.

    A gap between two consecutive required intervals is bridgeable iff it
    is at most ``max_gap_words`` long and contains no forbidden register.
    Blocks never cross an unbridgeable gap, so each cluster is planned
    independently.
    """
    cluster: list[int] = []
    prev_end: int | None = None
    for s, e in required:
        if prev_end is not None:
            gap_len = s - prev_end - 1
            if gap_len > max_gap_words or _touches(
                forbidden, forbidden_starts, prev_end + 1, s - 1
            ):
                yield cluster
                cluster = []
        cluster.extend(range(s, e + 1))
        prev_end = e
    if cluster:
        yield cluster


def _plan_cluster(regs: list[int], max_read_words: int) -> list[ReadBlock]:
    """Optimal blocks covering one bridgeable cluster of registers.

    Dynamic program over the sorted required registers.  ``far[i]`` is the
    farthest register a block starting at ``regs[i]`` may reach (length
    limit only -- gaps inside a cluster are all bridgeable).  ``cnt[i]``
    is the minimum block count for the suffix (greedy farthest-reach is
    optimal on a chain).  Among count-optimal first blocks, the extra
    registers of block ``[i, j]`` plus the optimal suffix ``j+1`` are
    minimized; ties keep the smallest ``j``, which yields the
    lexicographically smallest starts tuple (the next block then starts
    at the smallest possible register, and the suffix is itself optimal
    by induction).
    """
    n = len(regs)
    far = [0] * n
    j = 0
    for i in range(n):
        if j < i:
            j = i
        while j + 1 < n and regs[j + 1] - regs[i] + 1 <= max_read_words:
            j += 1
        far[i] = j
    cnt = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        cnt[i] = 1 + cnt[far[i] + 1]
    extra = [0] * (n + 1)
    choice = [0] * n
    for i in range(n - 1, -1, -1):
        target = cnt[i] - 1
        j = i
        # Valid first blocks end at j in [i, far[i]] with cnt[j+1] == target;
        # cnt is non-increasing, so skip the too-short reaches first.
        while cnt[j + 1] > target:
            j += 1
        best_key: int | None = None
        best_j = j
        while j <= far[i] and cnt[j + 1] == target:
            # extra of block [i, j] = (regs[j]-j) - (regs[i]-i); the
            # subtracted term is constant across candidates.
            key = regs[j] - j + extra[j + 1]
            if best_key is None or key < best_key:
                best_key = key
                best_j = j
            j += 1
        choice[i] = best_j
        extra[i] = (
            (regs[best_j] - regs[i] + 1) - (best_j - i + 1) + extra[best_j + 1]
        )
    blocks: list[ReadBlock] = []
    i = 0
    while i < n:
        j = choice[i]
        blocks.append(ReadBlock(regs[i], regs[j] - regs[i] + 1))
        i = j + 1
    return blocks


def plan_reads(
    required: object,
    *,
    max_read_words: int,
    max_gap_words: int,
    forbidden: object = (),
) -> ReadPlan:
    """Plan minimal read blocks covering the required registers.

    ``required`` is an iterable of inclusive ``(start, end)`` intervals of
    registers that must be read (overlapping intervals are merged, so
    overlapping fields are read once).  Raises :class:`ConfigError` --
    before any plan is returned -- if a required register lies inside a
    forbidden range: no legal plan exists, and retrying cannot help.
    """
    max_read_words = _check_int("max_read_words", max_read_words)
    if not 1 <= max_read_words <= MAX_ADDRESS + 1:
        raise ConfigError(
            f"max_read_words must be in 1..{MAX_ADDRESS + 1}, "
            f"got {max_read_words}"
        )
    max_gap_words = _check_int("max_gap_words", max_gap_words)
    if not 0 <= max_gap_words <= MAX_ADDRESS:
        raise ConfigError(
            f"max_gap_words must be in 0..{MAX_ADDRESS}, got {max_gap_words}"
        )
    req = normalize_intervals("required", required)
    if not req:
        raise ConfigError("required must contain at least one interval")
    forb = normalize_intervals("forbidden", forbidden)
    forb_starts = [s for s, _ in forb]
    for s, e in req:
        if _touches(forb, forb_starts, s, e):
            raise ConfigError(
                f"required registers {s}..{e} intersect a forbidden range; "
                "no legal read plan exists"
            )
    blocks: list[ReadBlock] = []
    for cluster in _clusters(req, forb, forb_starts, max_gap_words):
        blocks.extend(_plan_cluster(cluster, max_read_words))
    return ReadPlan(tuple(blocks), req, forb, max_read_words, max_gap_words)


def check_read_plan(
    plan: object,
    required: object,
    *,
    max_read_words: int,
    max_gap_words: int,
    forbidden: object = (),
) -> None:
    """Verify ``plan`` against the legality contract; raise ConfigError.

    Checks, in order: plan type; every block's shape (int fields, length
    in 1..``max_read_words``, inside the address space); blocks sorted
    and non-overlapping; no block touches a forbidden register; no block
    extends past the required span; every gap run inside a block is at
    most ``max_gap_words``; and every required register is covered.
    Returns ``None`` when the plan is legal.
    """
    if not isinstance(plan, ReadPlan):
        raise ConfigError(
            f"plan must be a ReadPlan, got {type(plan).__name__}"
        )
    max_read_words = _check_int("max_read_words", max_read_words)
    if not 1 <= max_read_words <= MAX_ADDRESS + 1:
        raise ConfigError(
            f"max_read_words must be in 1..{MAX_ADDRESS + 1}, "
            f"got {max_read_words}"
        )
    max_gap_words = _check_int("max_gap_words", max_gap_words)
    if not 0 <= max_gap_words <= MAX_ADDRESS:
        raise ConfigError(
            f"max_gap_words must be in 0..{MAX_ADDRESS}, got {max_gap_words}"
        )
    req = normalize_intervals("required", required)
    forb = normalize_intervals("forbidden", forbidden)
    forb_starts = [s for s, _ in forb]
    req_set: set[int] = set()
    for s, e in req:
        req_set.update(range(s, e + 1))
    covered: set[int] = set()
    prev_last = -1
    for i, block in enumerate(plan.blocks):
        if not isinstance(block, ReadBlock):
            raise ConfigError(
                f"blocks[{i}] must be a ReadBlock, "
                f"got {type(block).__name__}"
            )
        start = _check_int(f"blocks[{i}].start", block.start)
        count = _check_int(f"blocks[{i}].count", block.count)
        if count < 1:
            raise ConfigError(f"blocks[{i}] count must be >= 1, got {count}")
        if count > max_read_words:
            raise ConfigError(
                f"blocks[{i}] length {count} exceeds max_read_words "
                f"{max_read_words}"
            )
        if not 0 <= start or start + count - 1 > MAX_ADDRESS:
            raise ConfigError(
                f"blocks[{i}] = ({start}, {count}) outside "
                f"0..{MAX_ADDRESS}"
            )
        if start <= prev_last:
            raise ConfigError(
                f"blocks[{i}] starts at {start}, overlapping or out of "
                f"order (previous block ends at {prev_last})"
            )
        last = start + count - 1
        if _touches(forb, forb_starts, start, last):
            raise ConfigError(
                f"blocks[{i}] = ({start}, {count}) touches a forbidden "
                "register"
            )
        if start not in req_set or last not in req_set:
            raise ConfigError(
                f"blocks[{i}] = ({start}, {count}) extends past the "
                "required span"
            )
        run = 0
        for r in range(start, last + 1):
            if r in req_set:
                run = 0
            else:
                run += 1
                if run > max_gap_words:
                    raise ConfigError(
                        f"blocks[{i}] reads a gap of more than "
                        f"{max_gap_words} extra register(s) at register {r}"
                    )
        covered.update(range(start, last + 1))
        prev_last = last
    missing = sorted(req_set - covered)
    if missing:
        shown = ", ".join(str(r) for r in missing[:5])
        more = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        raise ConfigError(
            f"plan does not cover required register(s) {shown}{more}"
        )
