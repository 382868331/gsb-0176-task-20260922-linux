"""Barrier mirror tests: per-device last commit <= barrier, no field
mixing from updates during the read, snapshot_unavailable on insufficient
or pruned history, and all-or-nothing cross-device publication.

Expected register words are hand-computed from the documented
word/byte-order model (see tests/test_fields.py); the package's own
encoder is never used to produce expected values.  Devices are driven by
explicit stage/commit calls or fixed scripts -- no threads, no sleeps.
"""

import unittest
from fractions import Fraction

from regmirror import (
    Change,
    DeviceMirror,
    DeviceTarget,
    FieldSpec,
    HistoryDevice,
    Mirror,
    RegisterGroup,
    ScanConfig,
    Scanner,
    SnapshotUnavailableError,
)
from regmirror.errors import AddressError, ConfigError, DeviceReadError

# ---- Hand-computed register images -------------------------------------
# Boiler device (image size 16):
#   0..3  energy  u64, word big / byte big
#   4..5  temp    s32, word little / byte big, scale 1/10
#   6     version register
#   ENERGY_V1 = 0x0102030405060708 -> words 0102 0304 0506 0708
ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)
#   ENERGY_V2 = 0x0A0B0C0D0E0F1011 -> words 0A0B 0C0D 0E0F 1011
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)
#   temp raw 235  -> 0x000000EB, word little -> (0x00EB, 0x0000)
TEMP_V1 = (0x00EB, 0x0000)
#   temp raw -40  -> 0xFFFFFFD8, word little -> (0xFFD8, 0xFFFF)
TEMP_V2 = (0xFFD8, 0xFFFF)
ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec("temp", 4, width=32, signed=True, word_order="little",
                 scale=Fraction(1, 10))
BOILER = RegisterGroup("boiler", version_address=6,
                       fields=(ENERGY, TEMP))

# Turbine device (image size 16):
#   9..10 rpm  u32, word big / byte big; 8 = version register
#   raw 7 -> (0x0000, 0x0007); raw 9 -> (0x0000, 0x0009)
RPM_V1 = (0x0000, 0x0007)
RPM_V2 = (0x0000, 0x0009)
RPM = FieldSpec("rpm", 9, width=32)
TURBINE = RegisterGroup("turbine", version_address=8, fields=(RPM,))


def boiler_initial(version=0):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = version
    return regs


def turbine_initial(version=0):
    regs = {8: version, 9: RPM_V1[0], 10: RPM_V1[1]}
    return regs


def publish_boiler_v2(dev):
    """Stage and commit the V2 image on a boiler HistoryDevice."""
    dev.stage(Change.set_words(0, ENERGY_V2 + TEMP_V2))
    return dev.commit_now(6)


def publish_turbine_v2(dev):
    dev.stage(Change.set_words(9, RPM_V2))
    return dev.commit_now(8)


class TestBarrierSelection(unittest.TestCase):
    def test_last_commit_at_or_before_barrier(self):
        # Boiler: seq0 = V1, seq1 = V2.
        dev = HistoryDevice(16, initial=boiler_initial())
        s1 = publish_boiler_v2(dev)
        self.assertEqual((dev.first_sequence, dev.oldest_sequence,
                          dev.latest_sequence), (0, 0, 1))
        self.assertEqual(s1, 1)
        scn = Scanner()
        t = DeviceTarget("boiler", dev, [BOILER])
        # Barrier 0 -> seq 0 (V1).
        m0 = scn.mirror([t], barrier=0)
        self.assertEqual(m0.sequences, {"boiler": 0})
        g0 = m0["boiler"]["boiler"]
        self.assertEqual(g0["energy"].raw, 0x0102030405060708)
        self.assertEqual(g0["temp"].raw, 235)
        self.assertEqual(g0["temp"].value, Fraction(47, 2))
        # Barrier 1 -> seq 1 (V2).
        m1 = scn.mirror([t], barrier=1)
        self.assertEqual(m1.sequences, {"boiler": 1})
        g1 = m1["boiler"]["boiler"]
        self.assertEqual(g1["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(g1["temp"].raw, -40)
        self.assertEqual(g1["temp"].value, Fraction(-4))
        # Barrier beyond the latest commit -> still the last commit.
        m9 = scn.mirror([t], barrier=9)
        self.assertEqual(m9.sequences, {"boiler": 1})
        self.assertEqual(m9["boiler"]["boiler"]["energy"].raw,
                         0x0A0B0C0D0E0F1011)

    def test_per_device_independent_selection(self):
        # Two devices at different commit counts share one barrier:
        # boiler has seq 0..2, turbine only seq 0..1.
        boiler = HistoryDevice(16, initial=boiler_initial())
        turbine = HistoryDevice(16, initial=turbine_initial())
        publish_boiler_v2(boiler)
        publish_turbine_v2(turbine)
        # One more boiler-only commit with a different word pattern.
        boiler.stage(Change.set_words(0, (0x11, 0x22, 0x33, 0x44) + TEMP_V2))
        boiler.commit_now(6)
        scn = Scanner()
        m = scn.mirror(
            [DeviceTarget("b", boiler, [BOILER]),
             DeviceTarget("t", turbine, [TURBINE])],
            barrier=2,
        )
        self.assertEqual(m.sequences, {"b": 2, "t": 1})
        # seq 2 energy: words 0011 0022 0033 0044 by hand.
        self.assertEqual(m["b"]["boiler"]["energy"].raw,
                         0x0011002200330044)
        # temp words unchanged at V2 in that commit -> raw -40.
        self.assertEqual(m["b"]["boiler"]["temp"].raw, -40)
        self.assertEqual(m["t"]["turbine"]["rpm"].raw, 9)

    def test_chunked_historical_reads(self):
        # max_read_words=2 splits the 6-word boiler span into 3 chunks;
        # historical reads must still decode the whole commit.
        dev = HistoryDevice(16, initial=boiler_initial())
        publish_boiler_v2(dev)
        scn = Scanner(ScanConfig(max_read_words=2))
        m = scn.mirror([DeviceTarget("b", dev, [BOILER])], barrier=1)
        g = m["b"]["boiler"]
        self.assertEqual(g.version, 2)  # seq0 keeps initial word 0; first commit 0->2
        self.assertEqual(g["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(g["temp"].raw, -40)


class TestNoMixingDuringReads(unittest.TestCase):
    """Updates committed while the mirror reads must not enter its fields.

    The structural guarantee: a resolved sequence names one frozen image
    tuple; subsequent device commits replace the live image and add new
    history entries but never mutate that tuple.  These tests exercise it
    deterministically by firing commits from inside ``read_at``.
    """

    def test_uncommitted_staged_words_are_invisible(self):
        dev = HistoryDevice(16, initial=boiler_initial())
        publish_boiler_v2(dev)  # latest committed = seq1/V2
        # Stage V1-looking words again but never commit.
        dev.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
        m = Scanner().mirror(
            [DeviceTarget("b", dev, [BOILER])], barrier=99
        )
        d = m["b"]
        g = d["boiler"]
        self.assertEqual(d.sequence, 1)
        self.assertEqual(g["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(g["temp"].raw, -40)

    def test_commits_during_chunked_read_return_frozen_commit(self):
        # depth=2 device; the wrapper fires a brand-new commit from inside
        # every historical read.  The scanner must keep decoding the
        # pinned frozen commit, and the pin must keep it from eviction.
        underlying = HistoryDevice(16, initial=boiler_initial(),
                                   history_depth=2)
        publish_boiler_v2(underlying)  # seq1 = V2

        class CommittingReader:
            def __init__(self, d):
                self._d = d
                self.size = d.size
                self.fired = 0

            def resolve_barrier(self, barrier):
                return self._d.resolve_barrier(barrier)

            def pin(self, seq):
                self._d.pin(seq)

            def unpin(self, seq):
                self._d.unpin(seq)

            def retains(self, seq):
                return self._d.retains(seq)

            def read_at(self, seq, start, count):
                words = self._d.read_at(seq, start, count)
                # Commit after reading: the frozen image of `seq` must
                # survive even with depth=2.
                self._d.stage(
                    Change.set_words(
                        0,
                        ((0x90 + self.fired) & 0xFF, 0x00, 0x00, 0x00)
                        + TEMP_V1,
                    )
                )
                self._d.commit_now(6)
                self.fired += 1
                return words

        reader = CommittingReader(underlying)
        scn = Scanner(ScanConfig(max_read_words=2))
        # Independent oracle, recorded before the mirroring commits:
        # the frozen seq 1 words straight from retained history.
        oracle = tuple(underlying.read_at(1, a, 1)[0] for a in range(6))
        self.assertEqual(oracle, ENERGY_V2 + TEMP_V2)
        m = scn.mirror([DeviceTarget("b", reader, [BOILER])], barrier=1)
        d = m["b"]
        g = d["boiler"]
        # Served exactly the pinned seq 1 (V2), despite 4 commits fired
        # during the 4 historical reads (3 data chunks + version).
        self.assertEqual(d.sequence, 1)
        self.assertEqual(g["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(g["temp"].raw, -40)
        self.assertEqual(reader.fired, 4)

    def test_pinned_commit_cannot_be_pruned(self):
        dev = HistoryDevice(16, initial=boiler_initial(), history_depth=1)
        dev.pin(0)  # pin before any later commit can evict seq0
        try:
            publish_boiler_v2(dev)  # would prune seq0, but it is pinned
            self.assertTrue(dev.retains(0))
            dev.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
            dev.commit_now(6)  # still cannot evict the pinned commit
            self.assertEqual(dev.oldest_sequence, 0)
            self.assertEqual(
                dev.read_at(0, 0, 4), ENERGY_V1
            )
        finally:
            dev.unpin(0)
        # After release the retention window catches up: unpin prunes
        # down to [2]; the next commit leaves only seq 3.
        dev.stage(Change.set_words(0, ENERGY_V2 + TEMP_V2))
        dev.commit_now(6)
        self.assertFalse(dev.retains(0))
        self.assertEqual(dev.oldest_sequence, 3)

    def test_two_groups_share_one_commit(self):
        # One device, two groups whose version registers are bumped by a
        # single commit: both group snapshots must come from that commit.
        dev = HistoryDevice(
            16,
            initial={**boiler_initial(), **turbine_initial()},
        )
        publish_boiler_v2(dev)
        publish_turbine_v2(dev)
        # Joint commit: both groups change together.
        dev.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
        dev.stage(Change.set_words(9, RPM_V1))
        seq = dev.commit_now(6, 8)
        self.assertEqual(seq, 3)
        m = Scanner().mirror(
            [DeviceTarget("d", dev, [BOILER, TURBINE])], barrier=3
        )
        self.assertEqual(m["d"]["boiler"]["energy"].raw,
                         0x0102030405060708)
        self.assertEqual(m["d"]["turbine"]["rpm"].raw, 7)
        # Both group snapshots report the same device sequence.
        self.assertEqual(m["d"].sequence, 3)


class TestSnapshotUnavailable(unittest.TestCase):
    def test_device_without_history_is_insufficient(self):
        dev = HistoryDevice(16, initial_committed=False)
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            Scanner().mirror(
                [DeviceTarget("b", dev, [BOILER])], barrier=0
            )
        err = ctx.exception
        self.assertEqual(err.code, "snapshot_unavailable")
        self.assertEqual(err.reason, "history_insufficient")
        self.assertEqual(err.device, "b")
        self.assertEqual(err.barrier, 0)
        self.assertIsNone(err.oldest)

    def test_pruned_history_distinguished_from_never_existing(self):
        # depth=2; after seq0..2 the retained window is [1, 2].
        dev = HistoryDevice(16, initial=boiler_initial(), history_depth=2)
        publish_boiler_v2(dev)
        dev.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
        dev.commit_now(6)
        self.assertEqual(dev.retained_sequences, (1, 2))
        # Barrier 0 names a commit that existed but was pruned.
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            Scanner().mirror(
                [DeviceTarget("b", dev, [BOILER])], barrier=0
            )
        err = ctx.exception
        self.assertEqual(err.code, "snapshot_unavailable")
        self.assertEqual(err.reason, "history_pruned")
        self.assertEqual(err.oldest, 1)
        # Barrier 99 precedes the first ever commit? No: it is in the
        # future, hence served.  A negative barrier is ConfigError.
        m = Scanner().mirror(
            [DeviceTarget("b", dev, [BOILER])], barrier=99
        )
        self.assertEqual(m["b"].sequence, 2)

    def test_direct_history_api_reasons(self):
        dev = HistoryDevice(16, initial=boiler_initial(), history_depth=1)
        publish_boiler_v2(dev)  # seq0 pruned
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            dev.resolve_barrier(0)
        self.assertEqual(ctx.exception.reason, "history_pruned")
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            dev.read_at(0, 0, 1)
        self.assertEqual(ctx.exception.reason, "history_pruned")
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            dev.pin(0)
        self.assertEqual(ctx.exception.reason, "history_pruned")

    def test_any_device_failure_publishes_nothing(self):
        good = HistoryDevice(16, initial=boiler_initial())
        publish_boiler_v2(good)
        bad = HistoryDevice(16, initial_committed=False)
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            Scanner(ScanConfig(max_attempts=2)).mirror(
                [DeviceTarget("good", good, [BOILER]),
                 DeviceTarget("bad", bad, [TURBINE])],
                barrier=1,
            )
        err = ctx.exception
        self.assertEqual(err.device, "bad")
        self.assertEqual(err.reason, "history_insufficient")
        # The exception leaks no decoded partial values.
        self.assertFalse(hasattr(err, "devices"))
        self.assertFalse(hasattr(err, "snapshot"))
        self.assertFalse(hasattr(err, "values"))

    def test_pins_released_after_failure(self):
        # The good device's seq0 is pinned during resolution; failure of
        # the second device must release it, so with depth=1 later
        # commits can advance the retention window normally.
        good = HistoryDevice(16, initial=boiler_initial(), history_depth=1)
        publish_boiler_v2(good)  # window [1], seq0 already pruned
        bad = HistoryDevice(16, initial_committed=False)
        with self.assertRaises(SnapshotUnavailableError):
            Scanner().mirror(
                [DeviceTarget("good", good, [BOILER]),
                 DeviceTarget("bad", bad, [TURBINE])],
                barrier=1,
            )
        good.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
        good.commit_now(6)
        self.assertEqual(good.oldest_sequence, 2)

    def test_foreign_device_loses_history_mid_read(self):
        # A misbehaving history object: pin succeeds, but after the reads
        # the commit is gone.  The scanner's post-read recheck must fail
        # the whole mirror rather than publish.
        dev = HistoryDevice(16, initial=boiler_initial())

        class Liar:
            size = 16

            def resolve_barrier(self, barrier):
                return 0

            def pin(self, seq):
                pass

            def unpin(self, seq):
                pass

            def retains(self, seq):
                return False  # history vanished

            def read_at(self, seq, start, count):
                return dev.read_at(seq, start, count)

        with self.assertRaises(SnapshotUnavailableError) as ctx:
            Scanner(ScanConfig(max_read_words=2)).mirror(
                [DeviceTarget("liar", Liar(), [BOILER])], barrier=0
            )
        self.assertEqual(ctx.exception.device, "liar")
        self.assertEqual(ctx.exception.reason, "history_pruned")


class TestVersionSemanticsOfCommits(unittest.TestCase):
    def test_version_bumped_by_two_per_commit(self):
        dev = HistoryDevice(16, initial=boiler_initial(version=10))
        self.assertEqual(dev.read_at(0, 6, 1), (10,))
        s = publish_boiler_v2(dev)
        self.assertEqual(dev.read_at(s, 6, 1), (12,))
        m = Scanner().mirror([DeviceTarget("b", dev, [BOILER])], barrier=1)
        self.assertEqual(m["b"]["boiler"].version, 12)

    def test_version_wraparound_at_65534(self):
        # Initial even word 65534; first commit wraps to 0.
        dev = HistoryDevice(16, initial=boiler_initial(version=65534))
        seq = publish_boiler_v2(dev)
        self.assertEqual(dev.read_at(seq, 6, 1), (0,))
        m = Scanner().mirror([DeviceTarget("b", dev, [BOILER])], barrier=1)
        self.assertEqual(m["b"]["boiler"].version, 0)

    def test_odd_live_word_normalized_at_commit(self):
        # The live image may hold an odd "write in progress" word while
        # staging; publishing snaps it to the next even value.
        dev = HistoryDevice(16, initial=boiler_initial(version=65535))
        publish_boiler_v2(dev)
        self.assertEqual(dev.read_at(1, 6, 1), (2,))

    def test_commit_via_script_and_multiple_version_registers(self):
        # A Change.commit inside a script step publishes the step's
        # earlier writes atomically and bumps both version registers.
        dev = HistoryDevice(
            16,
            script=[
                (
                    Change.set_words(0, ENERGY_V2 + TEMP_V2),
                    Change.set_words(9, RPM_V2),
                    Change.commit(6, 8),
                ),
            ],
            initial={**boiler_initial(), **turbine_initial()},
        )
        # The step fires after the first live read.
        self.assertEqual(dev.latest_sequence, 0)
        dev.read(0, 1)
        self.assertEqual(dev.latest_sequence, 1)
        self.assertEqual(dev.read_at(1, 6, 1), (2,))
        self.assertEqual(dev.read_at(1, 8, 1), (2,))
        m = Scanner().mirror(
            [DeviceTarget("d", dev, [BOILER, TURBINE])], barrier=1
        )
        self.assertEqual(m["d"]["boiler"]["energy"].raw,
                         0x0A0B0C0D0E0F1011)
        self.assertEqual(m["d"]["turbine"]["rpm"].raw, 9)

    def test_plain_device_mirror_rejects_commit_changes(self):
        with self.assertRaises(TypeError):
            DeviceMirror(16, script=[Change.commit(6)])

    def test_commit_requires_version_addresses(self):
        dev = HistoryDevice(16)
        with self.assertRaises(ValueError):
            dev.commit_now()
        with self.assertRaises(ValueError):
            Change.commit()
        with self.assertRaises(ValueError):
            Change.commit(1, 1)


class TestMirrorValidation(unittest.TestCase):
    def _dev(self):
        return HistoryDevice(16, initial=boiler_initial())

    def test_target_construction_rejects_bad_inputs(self):
        dev = self._dev()
        with self.assertRaises(ConfigError):
            DeviceTarget("", dev, [BOILER])
        with self.assertRaises(ConfigError):
            DeviceTarget("x", None, [BOILER])
        with self.assertRaises(ConfigError):
            DeviceTarget("x", dev, [])
        with self.assertRaises(ConfigError):
            DeviceTarget("x", dev, [BOILER, BOILER])  # dup group names
        with self.assertRaises(ConfigError):
            DeviceTarget("x", dev, [object()])

    def test_targets_sequence_and_unique_names(self):
        dev = self._dev()
        t = DeviceTarget("x", dev, [BOILER])
        with self.assertRaises(ConfigError):
            Scanner().mirror("x", 0)
        with self.assertRaises(ConfigError):
            Scanner().mirror([], 0)
        with self.assertRaises(ConfigError):
            Scanner().mirror([t, t], 0)
        with self.assertRaises(ConfigError):
            Scanner().mirror([t], 0.5)
        with self.assertRaises(ConfigError):
            Scanner().mirror([t], True)
        with self.assertRaises(ConfigError):
            Scanner().mirror([t], -1)

    def test_history_protocol_required(self):
        class NoHistory:
            size = 16

        with self.assertRaises(ConfigError):
            Scanner().mirror(
                [DeviceTarget("x", NoHistory(), [BOILER])], 0
            )

    def test_address_errors_precede_history_access(self):
        # A group whose registers exceed the device image must fail at
        # validation, before resolve_barrier is ever called.
        class Counting(HistoryDevice):
            resolves = 0

            def resolve_barrier(self, barrier):
                type(self).resolves += 1
                return super().resolve_barrier(barrier)

        counting = Counting(8, initial=boiler_initial())  # TURBINE needs 8..10
        with self.assertRaises(AddressError):
            Scanner().mirror(
                [DeviceTarget("x", counting, [TURBINE])], 0
            )
        self.assertEqual(counting.resolves, 0)

    def test_bad_historical_words_raise_device_read_error(self):
        good = HistoryDevice(16, initial=boiler_initial())

        class ShortReader:
            size = 16

            def resolve_barrier(self, barrier):
                return 0

            def pin(self, seq):
                pass

            def unpin(self, seq):
                pass

            def retains(self, seq):
                return True

            def read_at(self, seq, start, count):
                return (0,) * (count - 1)  # violates the read contract

        with self.assertRaises(DeviceReadError):
            Scanner(ScanConfig(max_read_words=2)).mirror(
                [DeviceTarget("x", ShortReader(), [BOILER])], 0
            )

    def test_read_at_bounds(self):
        dev = self._dev()
        with self.assertRaises(AddressError):
            dev.read_at(0, 15, 2)
        with self.assertRaises(ValueError):
            dev.read_at(0, 0, 0)
        with self.assertRaises(TypeError):
            dev.read_at(0, True, 1)

    def test_history_depth_validation(self):
        with self.assertRaises(ValueError):
            HistoryDevice(16, history_depth=0)
        with self.assertRaises(TypeError):
            HistoryDevice(16, history_depth=True)


class TestBarrierEnumeration(unittest.TestCase):
    """Exhaustively enumerate barriers against an independent reference."""

    def test_every_barrier_against_reference(self):
        # 5 commits seq 0..4, each writing energy word 0 = 0x1000 + seq;
        # retention depth 3 -> retained window [2, 3, 4].
        dev = HistoryDevice(16, initial=boiler_initial(), history_depth=3)
        for s in range(1, 5):
            dev.stage(Change.set_words(0, (0x1000 + s, 0, 0, 0) + TEMP_V1))
            dev.commit_now(6)
        self.assertEqual(dev.retained_sequences, (2, 3, 4))
        scn = Scanner(ScanConfig(max_read_words=2))
        t = DeviceTarget("b", dev, [BOILER])

        # Independent reference, written only from the documented
        # "last retained commit <= barrier" rule -- not from the device.
        retained = (2, 3, 4)

        def reference(barrier):
            candidates = [s for s in retained if s <= barrier]
            if not candidates:
                # barrier 0/1 existed (first_ever = 0) but were pruned.
                return ("history_pruned", None)
            return ("ok", max(candidates))

        for barrier in range(0, 7):
            kind, seq = reference(barrier)
            with self.subTest(barrier=barrier):
                if kind == "history_pruned":
                    with self.assertRaises(SnapshotUnavailableError) as ctx:
                        scn.mirror([t], barrier)
                    self.assertEqual(ctx.exception.reason, "history_pruned")
                    self.assertEqual(ctx.exception.oldest, 2)
                else:
                    m = scn.mirror([t], barrier)
                    self.assertEqual(m["b"].sequence, seq)
                    # Hand-checked: word0 of seq s is 0x1000+s and the
                    # other three energy words are 0 -> raw is that << 48.
                    self.assertEqual(
                        m["b"]["boiler"]["energy"].raw,
                        (0x1000 + seq) << 48,
                    )


class TestMirrorResultShape(unittest.TestCase):
    def test_mirror_indexing_and_lengths(self):
        b = HistoryDevice(16, initial=boiler_initial())
        t = HistoryDevice(16, initial=turbine_initial())
        m = Scanner().mirror(
            [DeviceTarget("boiler", b, [BOILER]),
             DeviceTarget("turbine", t, [TURBINE])],
            barrier=0,
        )
        self.assertIsInstance(m, Mirror)
        self.assertEqual(len(m), 2)
        self.assertEqual(m.barrier, 0)
        self.assertEqual(len(m["boiler"]), 1)
        self.assertEqual(m["boiler"].name, "boiler")
        self.assertEqual(m["boiler"]["boiler"].name, "boiler")
        with self.assertRaises(KeyError):
            m["nope"]
        with self.assertRaises(KeyError):
            m["boiler"]["nope"]
        # Groups served from history carry attempts == 0 (no live retry).
        self.assertEqual(m["boiler"]["boiler"].attempts, 0)


if __name__ == "__main__":
    unittest.main()
