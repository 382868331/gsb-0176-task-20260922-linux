"""Planner tests: optimal read-block planning under length, gap and
forbidden-range constraints.

The DP in ``regmirror.planner`` is checked two independent ways:

1. A brute-force oracle enumerates *every* segmentation of the sorted
   covered registers (one block per segment) and takes the optimum under
   the same (reads, extra words, start tuple) key.
2. A second oracle enumerates *arbitrary disjoint integer intervals*
   (including blocks that start before / end after the extreme covered
   registers they contain -- "slack" blocks), proving that restricting
   block endpoints to covered-register boundaries never hides a better
   plan.

Neither oracle calls the planner; the field fixtures only supply
``address`` / ``last_address``.  All random layouts use fixed seeds.
"""

import random
import unittest

from regmirror import (
    FieldSpec,
    ReadBlock,
    RegisterGroup,
    ScanConfig,
    Scanner,
    normalize_forbidden_ranges,
    plan_group,
    plan_scan,
)
from regmirror.errors import AddressError, ConfigError


# ---------------------------------------------------------------------
# Minimal field/group stand-ins (no constructor validation here so the
# oracle can describe arbitrary covered-register sets).
# ---------------------------------------------------------------------


class FakeField:
    def __init__(self, address, n_words):
        self.address = address
        self._n = n_words

    @property
    def last_address(self):
        return self.address + self._n - 1


class FakeGroup:
    def __init__(self, name, version_address, covered, fields=None):
        self.name = name
        self.version_address = version_address
        if fields is None:
            # One fake field per covered register; sets overlap freely,
            # the planner must take the union regardless.
            fields = [FakeField(a, 1) for a in covered]
        self.fields = tuple(fields)


# ---------------------------------------------------------------------
# Oracles
# ---------------------------------------------------------------------


def oracle_segmentations(covered, max_read, max_gap, forbidden):
    """Minimum over partitions of covered register indices."""
    m = len(covered)
    best = None

    def split(prefix_starts, i, reads, extra):
        nonlocal best
        if i == m:
            key = (reads, extra, tuple(prefix_starts))
            if best is None or key < best[0]:
                best = (key, tuple(prefix_starts))
            return
        j = i
        while j < m:
            s, e = covered[i], covered[j]
            gap = (e - s + 1) - (j - i + 1)
            if e - s + 1 > max_read:
                break  # growing j only makes the span longer
            if gap <= max_gap and not _block_hits(s, e, forbidden):
                split(prefix_starts + [s], j + 1, reads + 1, extra + gap)
            j += 1

    def _block_hits(s, e, forbidden):
        return any(s < fe and fs <= e for fs, fe in forbidden)

    split([], 0, 0, 0)
    assert best is not None, "oracle found no cover"
    return best[0]


def oracle_intervals(covered, space, max_read, max_gap, forbidden):
    """Minimum over arbitrary disjoint interval covers inside [0, space).

    Enumerates slack: a block covering covered register ``addr`` may
    start anywhere after the previous block up to ``addr`` and extend to
    anywhere from ``addr`` up to ``space - 1`` (subject to constraints).
    """
    covered = sorted(covered)
    best = None

    def rec(idx, prev_end, starts, reads, words):
        nonlocal best
        if idx == len(covered):
            extra = words - len(covered)
            key = (reads, extra, tuple(starts))
            if best is None or key < best:
                best = key
            return
        addr = covered[idx]
        for s in range(prev_end + 1, addr + 1):
            for e in range(addr, space):
                n = e - s + 1
                if n > max_read:
                    break
                # how many covered registers [s..e] contains
                j = idx
                while j < len(covered) and covered[j] <= e:
                    j += 1
                extra = n - (j - idx)
                if extra > max_gap:
                    continue
                if any(s < fe and fs <= e for fs, fe in forbidden):
                    continue
                rec(j, e, starts + [s], reads + 1, words + n)

    rec(0, -1, [], 0, 0)
    assert best is not None, "oracle found no cover"
    return best


def dp_key(plan):
    return (plan.data_read_count, plan.extra_words, plan.block_starts)


# ---------------------------------------------------------------------
# Exhaustive small-space comparison
# ---------------------------------------------------------------------


def all_forbidden_sets(space):
    """Every subset of registers as (merged) forbidden ranges."""
    for mask in range(1 << space):
        chosen = [a for a in range(space) if mask & (1 << a)]
        if not chosen:
            yield ()
            continue
        ranges = []
        s = p = chosen[0]
        for a in chosen[1:]:
            if a == p + 1:
                p = a
            else:
                ranges.append((s, p + 1))
                s = p = a
        ranges.append((s, p + 1))
        yield tuple(ranges)


class TestExhaustiveSmallSpaces(unittest.TestCase):
    def test_every_layout_space_1_5(self):
        # Every covered subset of spaces 1..5, every forbidden subset,
        # every length/gap budget, compared against the segmentation
        # oracle (and the slack-inclusive oracle on spaces <= 4).
        for space in range(1, 6):
            for cmask in range(1, 1 << space):
                covered = tuple(
                    a for a in range(space) if cmask & (1 << a)
                )
                for fmask in range(1 << space):
                    forbidden = tuple(
                        (a, a + 1)
                        for a in range(space)
                        if fmask & (1 << a)
                    )
                    if any(
                        any(s <= a < e for s, e in forbidden)
                        for a in covered
                    ):
                        # A field register inside a hole is impossible
                        # by construction for every budget.
                        g = FakeGroup(
                            "g", version_address=space, covered=covered
                        )
                        with self.assertRaises(ConfigError):
                            plan_group(
                                g,
                                size=space + 1,
                                max_read_words=space + 1,
                                max_gap_words=space,
                                forbidden_ranges=forbidden,
                            )
                        continue
                    self._check(space, covered, forbidden)

    def _check(self, space, covered, forbidden):
        # Version lives just past the data space so it never collides.
        g = FakeGroup("g", version_address=space, covered=covered)
        for max_read in range(1, space + 2):
            for max_gap in range(0, space + 1):
                expected = oracle_segmentations(
                    covered, max_read, max_gap, forbidden
                )
                plan = plan_group(
                    g,
                    size=space + 1,
                    max_read_words=max_read,
                    max_gap_words=max_gap,
                    forbidden_ranges=forbidden,
                )
                self.assertEqual(dp_key(plan), expected)
                self._assert_valid(plan, covered, forbidden,
                                   max_read, max_gap)
                if space <= 4:
                    wide = oracle_intervals(
                        covered, space, max_read, max_gap, forbidden
                    )
                    self.assertEqual(dp_key(plan), wide)

    def _assert_valid(self, plan, covered, forbidden, max_read, max_gap):
        covered = set(covered)
        seen = set()
        prev_end = -1
        for b in plan.blocks:
            self.assertLessEqual(b.count, max_read)
            self.assertGreater(b.start, prev_end)  # ascending, disjoint
            inside = set(range(b.start, b.start + b.count))
            self.assertEqual(inside & forbidden_set(forbidden), set())
            self.assertLessEqual(
                b.count - len(inside & covered), max_gap
            )
            seen |= inside
            prev_end = b.end
        self.assertTrue(covered <= seen)


def forbidden_set(ranges):
    return {a for s, e in ranges for a in range(s, e)}


class TestRandomLayouts(unittest.TestCase):
    def test_random_vs_segmentation_oracle(self):
        rng = random.Random(20260925)
        for _ in range(400):
            space = rng.randint(2, 14)
            k = rng.randint(1, space)
            covered = tuple(sorted(rng.sample(range(space), k)))
            free = [a for a in range(space) if a not in covered]
            # Build 0..3 random-length hole intervals out of free space,
            # which exercises merged multi-register forbidden ranges.
            rng.shuffle(free)
            hole_regs = sorted(free[: rng.randint(0, len(free))])
            forbidden: list[tuple[int, int]] = []
            i = 0
            while i < len(hole_regs):
                length = rng.randint(1, 3)
                s = hole_regs[i]
                e = s
                j = i + 1
                while j < len(hole_regs) and hole_regs[j] == e + 1 and length > 1:
                    e += 1
                    j += 1
                    length -= 1
                forbidden.append((s, e + 1))
                i = j
            max_read = rng.randint(1, space + 2)
            max_gap = rng.randint(0, space)
            # Version lives just past the data space.
            g = FakeGroup("g", version_address=space, covered=covered)
            expected = oracle_segmentations(
                covered, max_read, max_gap, tuple(forbidden)
            )
            plan = plan_group(
                g,
                size=space + 1,
                max_read_words=max_read,
                max_gap_words=max_gap,
                forbidden_ranges=tuple(forbidden),
            )
            self.assertEqual(dp_key(plan), expected)



# ---------------------------------------------------------------------
# Hand-checked planning scenarios
# ---------------------------------------------------------------------


def real_group(name="g", version=10, fields=(), forbidden=(), overlap=False):
    return RegisterGroup(
        name, version, fields,
        allow_overlap=overlap, forbidden_ranges=forbidden,
    )


class TestGapMerging(unittest.TestCase):
    # Fields occupy 0..1 and 5..6; registers 2,3,4 are a 3-word gap.
    FIELDS = (FieldSpec("a", 0), FieldSpec("b", 5))

    def test_gap_budget_blocks_merge(self):
        g = real_group(fields=self.FIELDS)
        plan = plan_group(g, size=16, max_read_words=7, max_gap_words=3)
        # One read 0..6 (7 words, 3 extra) beats two reads.
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 7)]
        )
        self.assertEqual(plan.extra_words, 3)
        self.assertEqual(plan.data_read_count, 1)
        self.assertEqual(plan.attempt_reads, 3)

    def test_gap_budget_too_small_forces_split(self):
        g = real_group(fields=self.FIELDS)
        plan = plan_group(g, size=16, max_read_words=7, max_gap_words=2)
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 2), (5, 2)]
        )
        self.assertEqual(plan.extra_words, 0)
        self.assertEqual(plan.data_read_count, 2)

    def test_zero_gap_never_reads_gap_registers(self):
        g = real_group(fields=self.FIELDS)
        plan = plan_group(g, size=16, max_read_words=16, max_gap_words=0)
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 2), (5, 2)]
        )

    def test_reads_primary_over_extra_words(self):
        # Even though merging costs 3 extra registers, fewer reads wins.
        g = real_group(fields=self.FIELDS)
        plan = plan_group(g, size=16, max_read_words=7, max_gap_words=3)
        self.assertEqual(plan.data_read_count, 1)


class TestLexicographicTieBreak(unittest.TestCase):
    # Covered registers 0,2,4; blocks may span at most 3 words with at
    # most 1 gap word.  Two plans tie on (2 reads, 1 extra word):
    #   [0..2],[4] starts (0,4)   vs   [0],[2..4] starts (0,2)
    # -> lexicographically smallest start tuple wins: (0,2).
    def test_smallest_start_tuple_wins(self):
        g = FakeGroup("g", 9, covered=(0, 2, 4))
        plan = plan_group(
            g, size=16, max_read_words=3, max_gap_words=1
        )
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 1), (2, 3)]
        )
        self.assertEqual(plan.extra_words, 1)


class TestForbiddenRanges(unittest.TestCase):
    FIELDS = (FieldSpec("a", 0), FieldSpec("b", 4))  # 0..1 and 4..5

    def test_hole_forbids_merge(self):
        g = real_group(fields=self.FIELDS, forbidden=((2, 4),))
        plan = plan_group(g, size=16, max_read_words=6, max_gap_words=2)
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 2), (4, 2)]
        )

    def test_no_hole_allows_merge(self):
        g = real_group(fields=self.FIELDS)
        plan = plan_group(g, size=16, max_read_words=6, max_gap_words=2)
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(0, 6)])

    def test_explicit_range_overrides_group_ranges(self):
        g = real_group(fields=self.FIELDS, forbidden=((2, 4),))
        # Explicit empty argument overrides the group attribute: the
        # hole is ignored and the fields merge.
        plan = plan_group(
            g, size=16, max_read_words=6, max_gap_words=2,
            forbidden_ranges=(),
        )
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(0, 6)])

    def test_field_inside_hole_is_impossible(self):
        g = real_group(fields=self.FIELDS, forbidden=((1, 2),))
        with self.assertRaises(ConfigError):
            plan_group(g, size=16, max_read_words=16, max_gap_words=15)

    def test_version_inside_hole_is_impossible(self):
        g = real_group(version=6, fields=self.FIELDS, forbidden=((6, 7),))
        with self.assertRaises(ConfigError):
            plan_group(g, size=16, max_read_words=16, max_gap_words=15)

    def test_scan_never_touches_forbidden_registers(self):
        # A device that blows up if the scanner reads the hole.
        from regmirror import DeviceMirror

        g = real_group(version=7, fields=self.FIELDS, forbidden=((2, 4),))
        initial = {0: 1, 1: 2, 4: 3, 5: 4, 7: 2}

        class Guarded(DeviceMirror):
            def read(self, start, count):
                for a in range(start, start + count):
                    if 2 <= a < 4:
                        raise AssertionError(f"forbidden register {a} read")
                return super().read(start, count)

        dev = Guarded(16, initial=initial)
        snap = Scanner(
            ScanConfig(max_read_words=6, max_gap_words=2)
        ).scan(dev, [g])
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)

    def test_config_errors_precede_first_read(self):
        from regmirror import DeviceMirror

        g = real_group(fields=self.FIELDS, forbidden=((0, 1),))
        dev = DeviceMirror(16)
        with self.assertRaises(ConfigError):
            Scanner().scan(dev, [g])
        self.assertEqual(dev.read_count, 0)

    def test_normalize_merges_overlapping_and_adjacent(self):
        self.assertEqual(
            normalize_forbidden_ranges(((1, 3), (3, 5), (8, 9))),
            ((1, 5), (8, 9)),
        )

    def test_bad_ranges_rejected(self):
        with self.assertRaises(ConfigError):
            normalize_forbidden_ranges([(1,)])
        with self.assertRaises(ConfigError):
            normalize_forbidden_ranges([(2, 2)])
        with self.assertRaises(ConfigError):
            normalize_forbidden_ranges([(-1, 2)])
        with self.assertRaises(ConfigError):
            normalize_forbidden_ranges([(5, 65537)])
        with self.assertRaises(ConfigError):
            normalize_forbidden_ranges([(True, 2)])

    def test_block_straddling_hole_split_around_it(self):
        # 0..3 and 6..9 (u64 fields), hole at register 4 only; a single
        # 0..9 block is banned because it contains the hole, so the
        # u64s each get their own block.
        fields = (FieldSpec("a", 0, width=64), FieldSpec("b", 6, width=64))
        g = real_group(version=10, fields=fields, forbidden=((4, 5),))
        plan = plan_group(g, size=16, max_read_words=16, max_gap_words=5)
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 4), (6, 4)]
        )
        # Gap budget 0 forces the same split for a different reason.
        plan0 = plan_group(g, size=16, max_read_words=16, max_gap_words=0)
        self.assertEqual(
            [b.as_tuple() for b in plan0.blocks], [(0, 4), (6, 4)]
        )


class TestLengthBoundary(unittest.TestCase):
    FIELD = (FieldSpec("a", 0, width=64),)  # 4 words

    def test_exactly_max_length_legal(self):
        g = real_group(fields=self.FIELD)
        plan = plan_group(g, size=16, max_read_words=4, max_gap_words=0)
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(0, 4)])

    def test_shorter_reads_split_field_like_legacy_chunking(self):
        # Fields may straddle data blocks (the version gate guards
        # consistency).  max_read_words=3 splits the 4-word u64 into two
        # blocks of 1 + 3: both (1,3) and (3,1) splits tie on reads and
        # extra words, so the lexicographically smallest start tuple
        # picks (0,1)+(1,3).  max_read_words=1 yields four singletons.
        g = real_group(fields=self.FIELD)
        plan = plan_group(g, size=16, max_read_words=3, max_gap_words=0)
        self.assertEqual(
            [b.as_tuple() for b in plan.blocks], [(0, 1), (1, 3)]
        )
        plan1 = plan_group(g, size=16, max_read_words=1, max_gap_words=0)
        self.assertEqual(
            [b.as_tuple() for b in plan1.blocks],
            [(0, 1), (1, 1), (2, 1), (3, 1)],
        )

    def test_single_word_field_max_one(self):
        g = FakeGroup("g", 5, covered=(2,))
        plan = plan_group(g, size=16, max_read_words=1, max_gap_words=0)
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(2, 1)])


class TestOverlappingFields(unittest.TestCase):
    def test_overlap_requires_opt_in(self):
        fields = (
            FieldSpec("a", 0, width=64),  # 0..3
            FieldSpec("b", 2, width=64),  # 2..5, overlaps 2..3
        )
        with self.assertRaises(ConfigError):
            RegisterGroup("g", 8, fields)
        g = RegisterGroup("g", 8, fields, allow_overlap=True)
        self.assertTrue(g.allow_overlap)

    def test_overlap_covered_once(self):
        fields = (
            FieldSpec("a", 0, width=64),
            FieldSpec("b", 2, width=64),
        )
        g = RegisterGroup("g", 8, fields, allow_overlap=True)
        plan = plan_group(g, size=16, max_read_words=6, max_gap_words=2)
        # Union 0..5 = 6 registers, one block; NOT 8 words for two
        # separate u64 reads.
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(0, 6)])
        self.assertEqual(plan.covered_words, 6)
        self.assertEqual(plan.extra_words, 0)


class TestPlanScanAndGroups(unittest.TestCase):
    def test_scan_config_gap_validation(self):
        ScanConfig()  # default works
        ScanConfig(max_gap_words=0)
        ScanConfig(max_gap_words=65535)
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=-1)
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=65536)
        with self.assertRaises(ConfigError):
            ScanConfig(max_gap_words=True)

    def test_group_forbidden_ranges_validated(self):
        with self.assertRaises(ConfigError):
            RegisterGroup(
                "g", 4, (FieldSpec("a", 0),), forbidden_ranges=((1,),)
            )
        g = RegisterGroup(
            "g", 4, (FieldSpec("a", 0),),
            forbidden_ranges=((3, 5), (2, 4)),
        )
        # Overlapping ranges are canonicalised on construction.
        self.assertEqual(g.forbidden_ranges, ((2, 5),))

    def test_build_plan_validates_groups(self):
        from regmirror import DeviceMirror

        dev = DeviceMirror(16)
        scanner = Scanner()
        with self.assertRaises(ConfigError):
            scanner.build_plan(dev, [])
        with self.assertRaises(ConfigError):
            scanner.build_plan(dev, [object()])

        class BadSize:
            size = 0

            def read(self, s, c):
                raise AssertionError  # never reached

        with self.assertRaises(ConfigError):
            scanner.build_plan(BadSize(), [
                real_group(fields=(FieldSpec("a", 0),))
            ])

    def test_groups_never_merged(self):
        # Two groups whose data registers are adjacent; plans stay
        # independent with their own version reads.
        g1 = real_group(name="g1", version=4,
                        fields=(FieldSpec("a", 0),))
        g2 = real_group(name="g2", version=5,
                        fields=(FieldSpec("b", 2),))
        plan = plan_scan(
            [g1, g2], size=16, max_read_words=4, max_gap_words=1
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan["g1"].blocks, (ReadBlock(0, 2),))
        self.assertEqual(plan["g2"].blocks, (ReadBlock(2, 2),))
        self.assertEqual(plan["g1"].attempt_reads, 3)
        with self.assertRaises(KeyError):
            plan["nope"]

    def test_scanner_build_plan_is_read_only(self):
        from regmirror import DeviceMirror

        g = real_group(version=6, fields=(FieldSpec("a", 0),
                                          FieldSpec("b", 4)))
        dev = DeviceMirror(16)
        scanner = Scanner(ScanConfig(max_read_words=7, max_gap_words=3))
        plan = scanner.build_plan(dev, [g])
        self.assertEqual(dev.read_count, 0)
        # 0..1 and 4..5: one 6-word block with 2 extra gap registers.
        self.assertEqual(
            [b.as_tuple() for b in plan["g"].blocks], [(0, 6)]
        )

    def test_address_checks_still_fire(self):
        g = real_group(version=99, fields=(FieldSpec("a", 0),))
        with self.assertRaises(AddressError):
            plan_group(g, size=16, max_read_words=4, max_gap_words=0)
        g2 = real_group(version=0, fields=(FieldSpec("a", 20),))
        with self.assertRaises(AddressError):
            plan_group(g2, size=16, max_read_words=4, max_gap_words=0)


class TestDeviceConsistency(unittest.TestCase):
    """End-to-end checks against a DeviceMirror that changes between
    reads: the planned blocks execute in the planned order, every
    attempt re-runs the same plan, and decoded values stay consistent.
    """

    def test_planned_read_sequence_observed(self):
        from regmirror import Change, DeviceMirror

        # Fields 0..1 and 5..6, version at 7; hole 2..4 forbids merging
        # the data reads even though max_read_words would allow it.
        g = RegisterGroup(
            "g", 7,
            fields=(FieldSpec("a", 0), FieldSpec("b", 5)),
            forbidden_ranges=((2, 5),),
        )
        initial = {0: 1, 1: 2, 5: 3, 6: 4, 7: 2}

        class Recorder:
            size = 16

            def __init__(self, inner):
                self._inner = inner
                self.calls = []

            def read(self, start, count):
                self.calls.append((start, count))
                return self._inner.read(start, count)

        inner = DeviceMirror(16, initial=initial)
        dev = Recorder(inner)
        snap = Scanner(
            ScanConfig(max_read_words=9, max_gap_words=4)
        ).scan(dev, [g])
        # version, block 0..2, block 5..2, version -- exactly the plan,
        # and register 2..4 was never read.
        self.assertEqual(dev.calls, [(7, 1), (0, 2), (5, 2), (7, 1)])
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)

    def test_failed_attempt_rerun_same_plan_no_block_reuse(self):
        from regmirror import Change, DeviceMirror

        # max_gap_words=0 splits fields 0..1 and 5..6 into two blocks,
        # so one attempt is: v, [0..1], [5..6], v (4 reads).
        g = RegisterGroup(
            "g", 7, fields=(FieldSpec("a", 0), FieldSpec("b", 5))
        )
        initial = {0: 0x00, 1: 0x01, 5: 0x00, 6: 0x02, 7: 2}
        # Commit 2 -> 4 between the version read and the first data
        # block of attempt 1 only; after that the image is stable.
        script = [
            (
                Change.set_version(7, 3),
                Change.set_words(5, (0x00, 0x03)),
                Change.set_version(7, 4),
            ),
        ]
        dev = DeviceMirror(16, script=script, initial=initial)
        scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=6,
                                     max_gap_words=0))
        snap = scanner.scan(dev, [g])
        self.assertEqual(snap["g"].attempts, 2)
        self.assertEqual(snap["g"].version, 4)
        self.assertEqual(snap["g"]["b"].raw, 0x0003)
        # Two full attempts x 4 reads; attempt 2 did not reuse any
        # words gathered by attempt 1.
        self.assertEqual(dev.read_count, 8)

    def test_overlapping_fields_scanned_consistently(self):
        from regmirror import DeviceMirror

        # Two u64 fields sharing registers 2..3: union 0..5 read once.
        fields = (
            FieldSpec("a", 0, width=64),
            FieldSpec("b", 2, width=64),
        )
        g = RegisterGroup("g", 8, fields, allow_overlap=True)
        words_a = (0x1111, 0x2222, 0x3333, 0x4444)
        initial = dict(enumerate(words_a + (0x5555, 0x6666)))
        initial[8] = 2
        dev = DeviceMirror(16, initial=initial)
        snap = Scanner(ScanConfig(max_read_words=6, max_gap_words=0)).scan(
            dev, [g]
        )
        vals = snap["g"]
        self.assertEqual(vals["a"].raw, 0x1111222233334444)
        self.assertEqual(vals["b"].raw, 0x3333444455556666)
        self.assertEqual(dev.read_count, 3)  # v, one 6-word block, v

    def test_exact_max_length_block_executes_single_data_read(self):
        from regmirror import DeviceMirror

        g = RegisterGroup(
            "g", 4, fields=(FieldSpec("a", 0, width=64),)
        )
        dev = DeviceMirror(16, initial={0: 1, 1: 2, 2: 3, 3: 4, 4: 2})
        Scanner(ScanConfig(max_read_words=4, max_gap_words=0)).scan(
            dev, [g]
        )
        self.assertEqual(dev.read_count, 3)

    def test_version_address_inside_merged_block_as_gap_word(self):
        from regmirror import DeviceMirror

        # Fields 0..1 and 3..4; version register 2 lies in the gap.
        # With a generous gap budget the data merge into read(0, 5)
        # (register 2 is merely an ignored extra word), while the gate
        # still uses the two dedicated singleton version reads.
        g = RegisterGroup(
            "g", 2, fields=(FieldSpec("a", 0), FieldSpec("b", 3))
        )
        dev = DeviceMirror(
            16, initial={0: 1, 1: 2, 2: 6, 3: 3, 4: 4}
        )
        scanner = Scanner(ScanConfig(max_read_words=5, max_gap_words=1))
        plan = scanner.build_plan(dev, [g])["g"]
        self.assertEqual([b.as_tuple() for b in plan.blocks], [(0, 5)])
        snap = scanner.scan(dev, [g])
        self.assertEqual(snap["g"]["a"].raw, 0x00010002)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)
        self.assertEqual(snap["g"].version, 6)
        # But the version register itself can never be forbidden: the
        # two mandatory version reads have to touch it.
        strict = RegisterGroup(
            "g", 2, (FieldSpec("a", 0), FieldSpec("b", 3)),
            forbidden_ranges=((2, 3),),
        )
        with self.assertRaises(ConfigError):
            scanner.build_plan(dev, [strict])

    def test_wraparound_with_split_blocks(self):
        from regmirror import Change, DeviceMirror

        # u64 field 0..3, max_read_words=3 -> lex-optimal blocks
        # [0..1] and [1..3]; version 6 wraps 65534 -> 0 on attempt 1.
        g = RegisterGroup("g", 6, fields=(FieldSpec("a", 0, width=64),))
        new_words = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)
        dev = DeviceMirror(
            16,
            script=[
                (
                    Change.set_version(6, 65535),
                    Change.set_words(0, new_words),
                    Change.set_version(6, 0),
                ),
            ],
            initial={0: 0, 1: 0, 2: 0, 3: 0, 6: 65534},
        )
        snap = Scanner(
            ScanConfig(max_attempts=3, max_read_words=3, max_gap_words=0)
        ).scan(dev, [g])
        got = snap["g"]
        self.assertEqual(got.attempts, 2)
        self.assertEqual(got.version, 0)
        self.assertEqual(got["a"].raw, 0x0A0B0C0D0E0F1011)


class TestLargePlanPerformance(unittest.TestCase):
    def test_ten_thousand_registers_linear(self):
        rng = random.Random(7)
        covered = tuple(range(0, 10000, 2))  # 5000 regs, 1-word gaps
        holes = tuple((a + 1, a + 2) for a in range(100, 9000, 500))
        g = FakeGroup("g", 9999, covered=covered)
        plan = plan_group(
            g,
            size=10000,
            max_read_words=120,
            max_gap_words=20,
            forbidden_ranges=holes,
        )
        # Sanity: every block respects the three constraints.
        for b in plan.blocks:
            self.assertLessEqual(b.count, 120)
            cov = sum(1 for a in range(b.start, b.start + b.count)
                      if a % 2 == 0 and a < 10000)
            self.assertLessEqual(b.count - cov, 20)
            for s, e in holes:
                self.assertFalse(b.start < e and s <= b.end)


if __name__ == "__main__":
    unittest.main()
