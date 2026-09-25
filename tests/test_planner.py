"""Planner tests: block legality and optimality of the read planner.

Optimality is cross-checked the strong way: for small address spaces the
tests *enumerate every legal block combination independently* (a plain
brute-force partition reference that shares no code with the planner)
and assert the planner's result wins the fixed ordering -- fewest
blocks, then fewest extra registers, then lexicographically smallest
starts.
"""

import unittest

from regmirror import (
    Change,
    DeviceMirror,
    FieldSpec,
    InconsistentScanError,
    ReadBlock,
    ReadPlan,
    RegisterGroup,
    ScanConfig,
    Scanner,
    check_read_plan,
    normalize_intervals,
    plan_reads,
)
from regmirror.errors import ConfigError


def brute_best(required, forbidden, max_read_words, max_gap_words):
    """Best legal block tuple by exhaustive partition enumeration.

    Every legal cover is a partition of the sorted required registers
    into consecutive runs (a legal block starts and ends on a required
    register, blocks are disjoint and cover everything); enumerate all
    2**(m-1) cut patterns and reject blocks that break a limit.  This
    reference deliberately uses none of the planner's internals.
    Returns ``None`` when a required register is forbidden.
    """
    req = set()
    for s, e in required:
        req.update(range(s, e + 1))
    forb = set()
    for s, e in forbidden:
        forb.update(range(s, e + 1))
    if req & forb:
        return None
    regs = sorted(req)
    m = len(regs)

    def block_legal(i, j):
        s, t = regs[i], regs[j]
        if t - s + 1 > max_read_words:
            return False
        run = 0
        for r in range(s, t + 1):
            if r in forb:
                return False
            if r in req:
                run = 0
            else:
                run += 1
                if run > max_gap_words:
                    return False
        return True

    best = None
    best_key = None
    for cuts in range(0, 1 << (m - 1)):
        groups = []
        i = 0
        for k in range(m - 1):
            if cuts >> k & 1:
                groups.append((i, k + 1))
                i = k + 1
        groups.append((i, m))
        blocks = []
        legal = True
        for i, j in groups:
            if not block_legal(i, j - 1):
                legal = False
                break
            blocks.append((regs[i], regs[j - 1]))
        if not legal:
            continue
        key = (
            len(blocks),
            sum(t - s + 1 for s, t in blocks) - m,
            tuple(s for s, _ in blocks),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = tuple(blocks)
    return best


def runs(regs):
    """Collapse sorted registers into inclusive (start, end) runs."""
    regs = sorted(regs)
    if not regs:
        return ()
    out = []
    start = prev = regs[0]
    for r in regs[1:]:
        if r == prev + 1:
            prev = r
        else:
            out.append((start, prev))
            start = prev = r
    out.append((start, prev))
    return tuple(out)


class TestPlannerBasics(unittest.TestCase):
    def test_single_contiguous_span_one_block(self):
        plan = plan_reads([(0, 5)], max_read_words=16, max_gap_words=0)
        self.assertEqual([(b.start, b.count) for b in plan.blocks], [(0, 6)])
        self.assertEqual(plan.read_count, 1)
        self.assertEqual(plan.extra_registers, 0)
        plan.check()

    def test_max_length_exact_split(self):
        # 6 required registers, exactly 2 per block -> 3 blocks, no extra.
        plan = plan_reads([(0, 5)], max_read_words=2, max_gap_words=0)
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks],
            [(0, 2), (2, 2), (4, 2)],
        )
        self.assertEqual(plan.extra_registers, 0)
        plan.check()

    def test_max_length_boundary_one_word_overhang(self):
        # 5 registers, cap 2: 3 blocks needed, 0 extras for each of the
        # three size compositions (2,2,1)/(2,1,2)/(1,2,2); starts tie
        # break picks (0,1,3): blocks [0], [1,2], [3,4].
        plan = plan_reads([(0, 4)], max_read_words=2, max_gap_words=0)
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks],
            [(0, 1), (1, 2), (3, 2)],
        )
        self.assertEqual(plan.read_count, 3)
        plan.check()

    def test_gap_bridge_within_limit_merges_blocks(self):
        # Fields at 0..1 and 4..5: a 2-register gap (registers 2,3).
        plan = plan_reads(
            [(0, 1), (4, 5)], max_read_words=6, max_gap_words=2
        )
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 6)]
        )
        self.assertEqual(plan.read_count, 1)
        self.assertEqual(plan.extra_registers, 2)
        plan.check()

    def test_gap_too_large_splits(self):
        plan = plan_reads(
            [(0, 1), (4, 5)], max_read_words=6, max_gap_words=1
        )
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 2), (4, 2)]
        )
        self.assertEqual(plan.read_count, 2)
        plan.check()

    def test_forbidden_hole_splits_blocks(self):
        # Gap registers 2,3 are forbidden: even with max_gap_words=2 no
        # block may bridge them.
        plan = plan_reads(
            [(0, 1), (4, 5)],
            max_read_words=6,
            max_gap_words=2,
            forbidden=[(2, 3)],
        )
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 2), (4, 2)]
        )
        plan.check()

    def test_forbidden_required_register_is_impossible(self):
        with self.assertRaises(ConfigError):
            plan_reads(
                [(0, 5)],
                max_read_words=16,
                max_gap_words=0,
                forbidden=[(3, 3)],
            )

    def test_overlapping_intervals_merged(self):
        plan = plan_reads(
            [(0, 3), (2, 5), (5, 6)], max_read_words=16, max_gap_words=0
        )
        self.assertEqual(plan.required, ((0, 6),))
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 7)]
        )
        plan.check()

    def test_normalize_merges_adjacent(self):
        # (0,1) and (2,3) are adjacent -> merge; (5,6) stays separate
        # because register 4 lies between.
        self.assertEqual(
            normalize_intervals("x", [(5, 6), (0, 1), (2, 3)]),
            ((0, 3), (5, 6)),
        )
        # Overlapping input merges as well.
        self.assertEqual(
            normalize_intervals("x", [(0, 1), (1, 2), (2, 3)]),
            ((0, 3),),
        )

    def test_lexicographic_tiebreak_short_first_block(self):
        # Required 0..2, cap 2: two blocks needed; partitions
        # [0] | [1,2] starts (0,1) vs [0,1] | [2] starts (0,2).
        plan = plan_reads([(0, 2)], max_read_words=2, max_gap_words=0)
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 1), (1, 2)]
        )
        plan.check()

    def test_empty_required_rejected(self):
        with self.assertRaises(ConfigError):
            plan_reads([], max_read_words=2, max_gap_words=0)


class TestPlannerExhaustive(unittest.TestCase):
    """Enumerate all legal block combinations on a small space."""

    def test_matches_brute_force_on_all_small_layouts(self):
        # All disjoint (required, forbidden) pairs on addresses 0..7:
        # sum_k C(8,k) * 2**(8-k) = 3**8 = 6561 pairs, each crossed
        # with cap in 1..4 and gap in 0..3.
        cases = 0
        for req_bits in range(1, 1 << 8):
            req_regs = [r for r in range(8) if req_bits >> r & 1]
            free = [r for r in range(8) if not (req_bits >> r & 1)]
            for forb_bits in range(0, 1 << len(free)):
                forb_regs = [
                    free[i] for i in range(len(free))
                    if forb_bits >> i & 1
                ]
                required = runs(req_regs)
                forbidden = runs(forb_regs)
                for cap in (1, 2, 3, 4):
                    for gap in (0, 1, 2, 3):
                        cases += 1
                        expect = brute_best(
                            required, forbidden, cap, gap
                        )
                        if expect is None:
                            with self.assertRaises(ConfigError):
                                plan_reads(
                                    required,
                                    max_read_words=cap,
                                    max_gap_words=gap,
                                    forbidden=forbidden,
                                )
                            continue
                        plan = plan_reads(
                            required,
                            max_read_words=cap,
                            max_gap_words=gap,
                            forbidden=forbidden,
                        )
                        got = tuple(
                            (b.start, b.last) for b in plan.blocks
                        )
                        self.assertEqual(
                            got,
                            expect,
                            msg=(
                                f"req={req_regs} forb={forb_regs} "
                                f"cap={cap} gap={gap}"
                            ),
                        )
                        # Every produced plan must pass the independent
                        # legality checker.
                        plan.check()
        self.assertGreater(cases, 100_000)


class TestPlannerGapExtrasTradeoff(unittest.TestCase):
    def test_extras_minimized_after_block_count(self):
        # Required [0] and [3], cap 4.
        plan0 = plan_reads([(0, 0), (3, 3)], max_read_words=4,
                           max_gap_words=0)
        self.assertEqual(plan0.read_count, 2)
        self.assertEqual(plan0.extra_registers, 0)
        # gap=2: one block (0,4) bridges registers 1,2 -> 1 read,
        # 2 extras.
        plan2 = plan_reads([(0, 0), (3, 3)], max_read_words=4,
                           max_gap_words=2)
        self.assertEqual(
            [(b.start, b.count) for b in plan2.blocks], [(0, 4)]
        )
        self.assertEqual(plan2.read_count, 1)
        self.assertEqual(plan2.extra_registers, 2)
        plan0.check()
        plan2.check()

    def test_bridge_chosen_when_it_saves_a_read(self):
        # Required [0],[2], cap 3, gap 1: block (0,3) covers both with
        # 1 extra and saves a read, so it beats two blocks.
        plan = plan_reads([(0, 0), (2, 2)], max_read_words=3,
                          max_gap_words=1)
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 3)]
        )
        self.assertEqual(plan.extra_registers, 1)
        plan.check()


class TestPlanChecker(unittest.TestCase):
    def _plan(self, blocks, required=((0, 5),), forbidden=(), **kw):
        return ReadPlan(
            tuple(ReadBlock(s, c) for s, c in blocks),
            required, tuple(forbidden), **kw
        )

    def test_rejects_block_over_limit(self):
        p = self._plan([(0, 6)], max_read_words=5, max_gap_words=0)
        with self.assertRaises(ConfigError):
            p.check()

    def test_rejects_forbidden_touch(self):
        p = self._plan(
            [(0, 6)],
            required=((0, 1), (3, 5)),
            forbidden=((2, 2),),
            max_read_words=16,
            max_gap_words=0,
        )
        with self.assertRaises(ConfigError):
            p.check()

    def test_rejects_uncovered_register(self):
        p = self._plan(
            [(0, 3), (4, 1)], max_read_words=16, max_gap_words=0
        )
        # register 5 missing
        with self.assertRaises(ConfigError):
            p.check()

    def test_rejects_gap_run_over_limit(self):
        p = self._plan(
            [(0, 4), (4, 2)],
            required=((0, 0), (3, 5)),
            max_read_words=16,
            max_gap_words=0,
        )
        # First block has a 2-register gap (1,2) with max_gap 0.
        with self.assertRaises(ConfigError):
            p.check()

    def test_rejects_overlapping_blocks(self):
        p = self._plan(
            [(0, 3), (2, 4)], max_read_words=16, max_gap_words=0
        )
        with self.assertRaises(ConfigError):
            p.check()

    def test_rejects_block_extending_past_required(self):
        p = self._plan(
            [(0, 7)],
            required=((0, 5),),
            max_read_words=16,
            max_gap_words=2,
        )
        with self.assertRaises(ConfigError):
            p.check()

    def test_accepts_good_plan(self):
        p = self._plan(
            [(0, 3), (3, 3)], max_read_words=3, max_gap_words=0
        )
        self.assertIsNone(p.check())

    def test_check_function_rejects_non_plan(self):
        with self.assertRaises(ConfigError):
            check_read_plan(
                object(), [(0, 5)], max_read_words=16, max_gap_words=0
            )

    def test_check_function_bad_params(self):
        p = self._plan([(0, 6)], max_read_words=6, max_gap_words=0)
        with self.assertRaises(ConfigError):
            check_read_plan(
                p, [(0, 5)], max_read_words=0, max_gap_words=0
            )
        with self.assertRaises(ConfigError):
            check_read_plan(
                p, [(0, 5)], max_read_words=6, max_gap_words=-1
            )


class TestConfigValidation(unittest.TestCase):
    def test_bad_gap_params(self):
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=-1)
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=True)
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=65536)

    def test_bad_forbidden_ranges(self):
        with self.assertRaises(ConfigError):
            ScanConfig(forbidden_ranges=[(3, 1)])
        with self.assertRaises(ConfigError):
            ScanConfig(forbidden_ranges=[("x", 1)])
        with self.assertRaises(ConfigError):
            ScanConfig(forbidden_ranges=7)
        with self.assertRaises(ConfigError):
            ScanConfig(forbidden_ranges=[(1,)])

    def test_forbidden_ranges_normalized_on_config(self):
        cfg = ScanConfig(forbidden_ranges=[(5, 6), (3, 4)])
        self.assertEqual(cfg.forbidden_ranges, ((3, 6),))

    def test_planner_param_bounds(self):
        with self.assertRaises(ConfigError):
            plan_reads([(0, 1)], max_read_words=0, max_gap_words=0)
        with self.assertRaises(ConfigError):
            plan_reads([(0, 1)], max_read_words=True, max_gap_words=0)
        with self.assertRaises(ConfigError):
            plan_reads([(0, 1)], max_read_words=2, max_gap_words=-1)
        with self.assertRaises(ConfigError):
            plan_reads([(0, 70000)], max_read_words=2, max_gap_words=0)
        with self.assertRaises(ConfigError):
            plan_reads("not-iterable", max_read_words=2, max_gap_words=0)


class TestScannerWithPlanner(unittest.TestCase):
    def _group(self):
        return RegisterGroup(
            "g",
            version_address=6,
            fields=(
                FieldSpec("a", 0, width=32),
                FieldSpec("b", 4, width=32),
            ),
        )

    def test_version_register_never_in_data_block(self):
        # Fields at 0..1 and 4..5, version at 6 adjacent; even with a
        # generous gap and cap, no block may include register 6.
        dev = DeviceMirror(8, initial={0: 1, 1: 2, 4: 3, 5: 4, 6: 2})
        cfg = ScanConfig(max_read_words=8, max_gap_words=2)
        plan = Scanner(cfg).plan_scan(dev, [self._group()])["g"]
        for b in plan.blocks:
            self.assertFalse(b.start <= 6 <= b.last)
        # Gap between 1 and 4 is registers 2,3 (length 2): bridgeable.
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 6)]
        )

    def test_forbidden_range_splits_scan_blocks(self):
        dev = DeviceMirror(
            8, initial={**{i: i + 1 for i in range(6)}, 6: 2}
        )
        cfg = ScanConfig(
            max_read_words=8, max_gap_words=2, forbidden_ranges=[(2, 3)]
        )
        plan = Scanner(cfg).plan_scan(dev, [self._group()])["g"]
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 2), (4, 2)]
        )
        snap = Scanner(cfg).scan(dev, [self._group()])
        self.assertEqual(snap["g"].version, 2)
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00050006)

    def test_no_plan_no_device_read(self):
        # A required register forbidden -> impossible by construction;
        # ConfigError before any read.
        dev = DeviceMirror(8)
        cfg = ScanConfig(forbidden_ranges=[(1, 1)])
        with self.assertRaises(ConfigError):
            Scanner(cfg).scan(dev, [self._group()])
        self.assertEqual(dev.read_count, 0)

    def test_plans_are_per_group(self):
        g1 = RegisterGroup("g1", 2, fields=(FieldSpec("a", 0, width=32),))
        g2 = RegisterGroup("g2", 6, fields=(FieldSpec("c", 4, width=32),))
        # Groups are adjacent-ish (g1 fields 0..1, g2 fields 4..5);
        # blocks must never merge across groups.
        dev = DeviceMirror(
            8, initial={0: 1, 1: 2, 4: 3, 5: 4, 2: 2, 6: 2}
        )
        scanner = Scanner(ScanConfig(max_read_words=16, max_gap_words=4))
        plans = scanner.plan_scan(dev, [g1, g2])
        self.assertEqual(
            [(b.start, b.count) for b in plans["g1"].blocks], [(0, 2)]
        )
        self.assertEqual(
            [(b.start, b.count) for b in plans["g2"].blocks], [(4, 2)]
        )
        snap = scanner.scan(dev, [g1, g2])
        self.assertEqual(snap["g1"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g2"]["c"].raw, 0x00030004)

    def test_overlapping_fields_scanned_once_each_word(self):
        # u64 at 0..3 and u32 at 2..3 overlap; one block reads all four.
        group = RegisterGroup(
            "ov", 8,
            fields=(
                FieldSpec("big", 0, width=64),
                FieldSpec("small", 2, width=32),
            ),
        )
        dev = DeviceMirror(
            16,
            initial={0: 1, 1: 2, 2: 3, 3: 4, 8: 2},
        )
        plan = Scanner(ScanConfig(max_read_words=4)).plan_scan(
            dev, [group]
        )["ov"]
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 4)]
        )
        snap = Scanner(ScanConfig(max_read_words=4)).scan(dev, [group])
        self.assertEqual(snap["ov"]["big"].raw, 0x0001000200030004)
        self.assertEqual(snap["ov"]["small"].raw, 0x00030004)


class _ReadLogger:
    """Record every (start, count) read for plan-observance assertions."""

    def __init__(self, dev):
        self._dev = dev
        self.size = dev.size
        self.calls = []

    def read(self, start, count):
        self.calls.append((start, count))
        return self._dev.read(start, count)


class TestPlannerDeviceConsistency(unittest.TestCase):
    """End-to-end: changing device script vs. the planned block layout."""

    def test_overlapping_fields_torn_then_retry_commits(self):
        # u64 at 0..3 overlaps u32 at 2..3.  max_read_words=2 forces
        # blocks [0,1],[2,3]: the u64 straddles them.  The device
        # rewrites the whole group (2 -> 4) right after the first data
        # block, so attempt 1 stitches a torn u64 and the gate rejects;
        # retry commits the new state decoded by BOTH fields.
        group = RegisterGroup(
            "ov", 8,
            fields=(
                FieldSpec("big", 0, width=64),
                FieldSpec("small", 2, width=32),
            ),
        )
        v1 = (0x01, 0x02, 0x03, 0x04)
        v2 = (0x0A, 0x0B, 0x0C, 0x0D)
        dev = DeviceMirror(
            16,
            script=[
                (),  # after r0 (version): nothing
                (
                    Change.set_version(8, 3),
                    Change.set_words(0, v2),
                    Change.set_version(8, 4),
                ),  # after r1 (block [0,1]): torn point
            ],
            initial={**dict(enumerate(v1)), 8: 2},
        )
        logger = _ReadLogger(dev)
        snap = Scanner(
            ScanConfig(max_attempts=3, max_read_words=2)
        ).scan(logger, [group])
        g = snap["ov"]
        self.assertEqual(g.attempts, 2)
        self.assertEqual(g.version, 4)
        self.assertEqual(g["big"].raw, 0x000A000B000C000D)
        self.assertEqual(g["small"].raw, 0x000C000D)
        # Both attempts issued exactly the planned block sequence:
        # version, [0,2), [2,4), version -- repeated in full on retry.
        self.assertEqual(
            logger.calls,
            [(8, 1), (0, 2), (2, 2), (8, 1)] * 2,
        )

    def test_forbidden_hole_with_version_wraparound(self):
        # Fields at 0..1 and 4..5; forbidden hole 2..3 splits the plan
        # into two blocks.  Version starts at 65534 (even); a commit
        # between the two version reads of attempt 1 wraps it to 0 and
        # rewrites the data, so attempt 1 is rejected and the retry
        # commits version 0 with the new consistent data.
        group = RegisterGroup(
            "g", 6,
            fields=(
                FieldSpec("a", 0, width=32),
                FieldSpec("b", 4, width=32),
            ),
        )
        old = {0: 1, 1: 2, 4: 3, 5: 4, 6: 65534}
        new_words = (0x11, 0x22, 0x33, 0x44)
        dev = DeviceMirror(
            8,
            script=[
                (
                    Change.set_version(6, 65535),
                    Change.set_words(0, new_words[:2]),
                    Change.set_words(4, new_words[2:]),
                    Change.set_version(6, 0),
                ),  # after r0 (first version read)
            ],
            initial=old,
        )
        cfg = ScanConfig(
            max_attempts=3,
            max_read_words=8,
            max_gap_words=2,
            forbidden_ranges=[(2, 3)],
        )
        plan = Scanner(cfg).plan_scan(dev, [group])["g"]
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks],
            [(0, 2), (4, 2)],
        )
        snap = Scanner(cfg).scan(dev, [group])
        g = snap["g"]
        self.assertEqual(g.attempts, 2)
        self.assertEqual(g.version, 0)
        # Two 16-bit words 0x11,0x22 decode to the 32-bit raw 0x00110022.
        self.assertEqual(g["a"].raw, 0x00110022)
        self.assertEqual(g["b"].raw, 0x00330044)

    def test_failed_attempt_restarts_same_plan_no_partial_reuse(self):
        # Two data blocks; the device changes after the second block of
        # each attempt so the gate fails twice.  Every attempt must run
        # the FULL block sequence from scratch (version first), never
        # resuming from a previous attempt's partial blocks.
        group = RegisterGroup(
            "g", 6,
            fields=(
                FieldSpec("a", 0, width=32),
                FieldSpec("b", 4, width=32),
            ),
        )
        dev = DeviceMirror(
            8,
            script=[
                (),  # r0 version
                (),  # r1 block [0,2)
                Change.set_version(6, 4),  # after r2: v1=2 != v2=4
                (),  # r3 version (attempt 2 start)
                (),  # r4 block 1
                Change.set_version(6, 6),  # after r5
            ],
            initial={0: 1, 1: 2, 4: 3, 5: 4, 6: 2},
        )
        cfg = ScanConfig(
            max_attempts=2,
            max_read_words=2,
            max_gap_words=0,
        )
        logger = _ReadLogger(dev)
        with self.assertRaises(InconsistentScanError) as ctx:
            Scanner(cfg).scan(logger, [group])
        self.assertEqual(ctx.exception.attempts, 2)
        # Two complete attempts, identical planned sequences.
        self.assertEqual(
            logger.calls,
            [(6, 1), (0, 2), (4, 2), (6, 1)] * 2,
        )

    def test_gap_bridge_single_block_consistent_commit(self):
        # max_gap_words=2 bridges registers 2,3 into one data block;
        # a stable device commits on attempt 1 with exactly one data
        # read (plus the two version reads).
        group = RegisterGroup(
            "g", 8,
            fields=(
                FieldSpec("a", 0, width=32),
                FieldSpec("b", 4, width=32),
            ),
        )
        dev = DeviceMirror(
            16,
            initial={0: 1, 1: 2, 4: 3, 5: 4, 8: 2},
        )
        logger = _ReadLogger(dev)
        snap = Scanner(
            ScanConfig(max_read_words=8, max_gap_words=2)
        ).scan(logger, [group])
        self.assertEqual(snap["g"].attempts, 1)
        self.assertEqual(logger.calls, [(8, 1), (0, 6), (8, 1)])
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)

    def test_version_in_gap_is_never_part_of_a_data_block(self):
        # Fields at 0..1 and 4..5 with the version register at 2 (in
        # the middle of the bridgeable gap).  Even with cap 8 and gap 4,
        # data blocks must not include register 2: the version check is
        # its own read and cannot be folded into a data block.  The
        # bridge is cut at the version register; register 3 (the other
        # gap word) may still be included by either side's block.
        group = RegisterGroup(
            "g", 2,
            fields=(
                FieldSpec("a", 0, width=32),
                FieldSpec("b", 4, width=32),
            ),
        )
        dev = DeviceMirror(
            8, initial={0: 1, 1: 2, 3: 9, 4: 3, 5: 4, 2: 2}
        )
        cfg = ScanConfig(max_read_words=8, max_gap_words=4)
        plan = Scanner(cfg).plan_scan(dev, [group])["g"]
        for blk in plan.blocks:
            self.assertFalse(blk.start <= 2 <= blk.last)
        # Cluster split by forbidden version register: left [0,1] and
        # right [4,5] planned independently.
        self.assertEqual(
            [(blk.start, blk.count) for blk in plan.blocks],
            [(0, 2), (4, 2)],
        )
        logger = _ReadLogger(dev)
        snap = Scanner(cfg).scan(logger, [group])
        self.assertEqual(snap["g"].version, 2)
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)
        # Every attempt: version read first, data blocks, version last.
        self.assertEqual(logger.calls[0], (2, 1))
        self.assertEqual(logger.calls[-1], (2, 1))
        self.assertEqual(
            logger.calls[1:-1], [(0, 2), (4, 2)]
        )


if __name__ == "__main__":
    unittest.main()
