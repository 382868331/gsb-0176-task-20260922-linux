"""Barrier-mirror tests: per-device last-commit-at-or-before-barrier,
field-consistent live reads vs immutable history, non-contiguous global
seqs, snapshot_unavailable (pre-scan and mid-scan), device read faults,
retry exhaustion, and the cross-device all-or-nothing policy.

All expected field words are hand-computed (see test_fields.py for the
endianness model); expected results never come from the scanner itself.
"""

import unittest
from fractions import Fraction

from regmirror import (
    Change,
    CommitStore,
    DeviceCommit,
    FieldSpec,
    HistoricalDevice,
    InconsistentMirrorScanError,
    MirrorRequest,
    MirrorScanner,
    MultiDeviceReadError,
    RegisterGroup,
    ScanConfig,
    SnapshotUnavailableError,
)
from regmirror.errors import AddressError, ConfigError, DeviceReadError

# Image size 16: 0..3 u64 energy, 4..5 s32 temp (word little), 6 version.
ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec(
    "temp", 4, width=32, signed=True, word_order="little",
    scale=Fraction(1, 10),
)
GROUP = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))

# Hand-computed images.
ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)  # 0x0102030405060708
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)  # 0x0A0B0C0D0E0F1011
TEMP_V1 = (0x00EB, 0x0000)                    # raw 235
TEMP_V2 = (0xFFD8, 0xFFFF)                    # raw -40
ALL_V1 = ENERGY_V1 + TEMP_V1
ALL_V2 = ENERGY_V2 + TEMP_V2

# The forbidden stitched value when V1 high words meet V2 low words.
TORN_ENERGY = 0x010203040E0F1011


def image_v1():
    return dict(enumerate(ALL_V1))


def make_device(device_id, start_seq, version=2, **kw):
    return HistoricalDevice(
        device_id, 16, 6, initial_version=version, initial=image_v1(),
        start_seq=start_seq, **kw
    )


def commit_v2(seq, version=4):
    return DeviceCommit.at(version, [Change.set_words(0, ALL_V2)], seq=seq)


def drain(dev, n):
    """Advance the device script by n reads without a scanner."""
    for _ in range(n):
        dev.read(0, 1)


class TestBarrierSelection(unittest.TestCase):
    def setUp(self):
        # A: commits at global seqs 1 (V1, v2), 5 (V2, v4), 9 (V1, v6).
        self.a = make_device(
            "A", 1,
            script=[commit_v2(5), DeviceCommit.at(6, [Change.set_words(0, ALL_V1)], seq=9)],
        )
        drain(self.a, 2)
        # B: one commit at global seq 2 (V1, v2), no further movement.
        self.b = make_device("B", 2)
        self.devices = {"A": self.a, "B": self.b}
        self.groups = {"A": GROUP, "B": GROUP}

    def scan(self, barrier, **cfg):
        req = MirrorRequest("plant", barrier, self.devices, self.groups)
        return MirrorScanner(ScanConfig(**cfg)).snapshot_at(req)

    def test_non_contiguous_seqs_last_commit_at_or_before(self):
        reads_a_before = self.a.read_count
        snap = self.scan(6, max_read_words=8)
        self.assertEqual(snap.status, "published")
        self.assertEqual(len(snap), 2)
        da, db = snap["A"], snap["B"]
        # A's last seq <= 6 is 5 (V2); B's is 2 (V1).
        self.assertEqual((da.seq, da.version, da.source), (5, 4, "history"))
        self.assertEqual((db.seq, db.version, db.source), (2, 2, "live"))
        # Sealed history needs zero device reads; B takes one gated pass.
        self.assertEqual(self.a.read_count, reads_a_before)
        self.assertEqual(db.attempts, 1)

    def test_all_fields_of_each_commit_are_returned(self):
        snap = self.scan(6)
        da = snap["A"]
        self.assertEqual({v.name for v in da.values}, {"energy", "temp"})
        self.assertEqual(da["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(da["temp"].raw, -40)
        self.assertEqual(da["temp"].value, Fraction(-4))
        db = snap["B"]
        self.assertEqual(db["energy"].raw, 0x0102030405060708)
        self.assertEqual(db["temp"].raw, 235)
        self.assertEqual(db["temp"].value, Fraction(47, 2))
        self.assertAlmostEqual(db["temp"].value_float, 23.5, places=12)

    def test_barrier_between_commits_and_at_exact_commit(self):
        # Single device A here: B starts at seq 2 and would make barriers
        # below 2 unavailable for the mirror as a whole.
        for barrier, seq in ((1, 1), (4, 1), (5, 5), (8, 5), (9, 9)):
            with self.subTest(barrier=barrier):
                req = MirrorRequest(
                    "m", barrier, {"A": self.a}, {"A": GROUP}
                )
                snap = MirrorScanner().snapshot_at(req)
                self.assertEqual(snap["A"].seq, seq)

    def test_barrier_above_everything_reads_live(self):
        snap = self.scan(1_000_000)
        for d in snap.devices:
            self.assertEqual(d.source, "live")
        self.assertEqual(snap["A"].seq, 9)
        self.assertEqual(snap["A"]["energy"].raw, 0x0102030405060708)


class TestLiveTornReads(unittest.TestCase):
    def test_torn_field_rejected_then_new_commit_commits(self):
        # Barrier 10; device commits seq 5 (V2) right after the first
        # data chunk of attempt 1.  Attempt 1 stitches V1 high onto V2
        # low -> gate 2 -> 4 rejects.  Attempt 2 gates on V2 cleanly.
        dev = make_device(
            "A", 1,
            script=[(), commit_v2(5)],
        )
        req = MirrorRequest(
            "m", 10, {"A": dev}, {"A": GROUP}
        )
        snap = MirrorScanner(ScanConfig(max_attempts=3, max_read_words=2)).snapshot_at(req)
        da = snap["A"]
        self.assertEqual((da.seq, da.source, da.attempts), (5, "live", 2))
        self.assertEqual(da["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertNotEqual(da["energy"].raw, TORN_ENERGY)
        # 2 attempts x 5 reads (version + 3 chunks + version).
        self.assertEqual(dev.read_count, 10)

    def test_update_past_barrier_cannot_mix_into_field(self):
        # Barrier pinned at 4.  The mid-scan commit is seq 5 > barrier,
        # so after the rejected attempt the scanner serves the sealed
        # seq-1 record: purely the OLD state, no mixed field.
        dev = make_device("A", 1, script=[(), commit_v2(5)])
        req = MirrorRequest("m", 4, {"A": dev}, {"A": GROUP})
        snap = MirrorScanner(ScanConfig(max_attempts=3, max_read_words=2)).snapshot_at(req)
        da = snap["A"]
        self.assertEqual((da.seq, da.source), (1, "history"))
        self.assertEqual(da["energy"].raw, 0x0102030405060708)
        self.assertEqual(da["temp"].raw, 235)
        self.assertNotEqual(da["energy"].raw, TORN_ENERGY)


class TestSnapshotUnavailable(unittest.TestCase):
    def test_barrier_older_than_retained_history(self):
        a = make_device("A", 1, max_records=2,
                        script=[commit_v2(5), commit_v2(9, version=6)])
        drain(a, 2)  # retains only seqs 5..9
        b = make_device("B", 2, max_records=2)
        reads_a_before = a.read_count
        req = MirrorRequest("m", 4, {"A": a, "B": b},
                            {"A": GROUP, "B": GROUP})
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            MirrorScanner().snapshot_at(req)
        err = ctx.exception
        self.assertEqual(err.status, "snapshot_unavailable")
        self.assertEqual(err.barrier, 4)
        self.assertEqual([m.device_id for m in err.missing], ["A"])
        self.assertEqual(err.missing[0].oldest_seq, 5)
        # Pre-flight history failure happens before any device read.
        self.assertEqual(a.read_count, reads_a_before)
        self.assertEqual(b.read_count, 0)

    def test_barrier_zero_with_first_seq_one(self):
        a = make_device("A", 1)
        req = MirrorRequest("m", 0, {"A": a}, {"A": GROUP})
        with self.assertRaises(SnapshotUnavailableError):
            MirrorScanner().snapshot_at(req)

    def test_several_devices_missing_listed(self):
        a = make_device("A", 7)
        b = make_device("B", 8)
        req = MirrorRequest("m", 6, {"A": a, "B": b},
                            {"A": GROUP, "B": GROUP})
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            MirrorScanner().snapshot_at(req)
        self.assertEqual(
            sorted(m.device_id for m in ctx.exception.missing), ["A", "B"]
        )

    def test_history_pruned_mid_scan_is_unavailable(self):
        # Good device first, pruning device second.  P keeps one record;
        # its first attempt reads v=2, then the script commits seq 6 and
        # prunes seq 5 (the barrier target); the gate fails, and the
        # retry can no longer find seq <= 5.
        good = make_device("good", 5)
        pruning = HistoricalDevice(
            "P", 16, 6, initial_version=2, initial=image_v1(),
            start_seq=5, max_records=1,
            script=[DeviceCommit.at(4, [Change.set_words(0, ALL_V2)], seq=6)],
        )
        req = MirrorRequest(
            "m", 5, {"good": good, "P": pruning},
            {"good": GROUP, "P": GROUP},
        )
        scanner = MirrorScanner(ScanConfig(max_attempts=3, max_read_words=2))
        with self.assertRaises(SnapshotUnavailableError) as ctx:
            scanner.snapshot_at(req)
        missing = ctx.exception.missing
        self.assertEqual([m.device_id for m in missing], ["P"])
        self.assertEqual(missing[0].oldest_seq, 6)
        # Even though "good" was fully resolved, nothing was published.
        self.assertEqual(pruning.history.newest.seq, 6)


class TestDeviceReadFailure(unittest.TestCase):
    def test_one_faulting_device_fails_whole_mirror(self):
        good = make_device("good", 1)
        bad = make_device(
            "bad", 1, faults={0: DeviceReadError("simulated bus fault")}
        )
        req = MirrorRequest(
            "m", 1, {"good": good, "bad": bad},
            {"good": GROUP, "bad": GROUP},
        )
        with self.assertRaises(MultiDeviceReadError) as ctx:
            MirrorScanner().snapshot_at(req)
        err = ctx.exception
        self.assertEqual(err.status, "device_read_error")
        self.assertEqual([f.device_id for f in err.failures], ["bad"])
        self.assertIn("simulated bus fault", str(err.failures[0].error))
        # Diagnostic carries no decoded values from the good device.
        self.assertFalse(hasattr(err, "values"))
        self.assertFalse(hasattr(err, "snapshot"))

    def test_device_returning_bad_words_fails_whole_mirror(self):
        class ShortDevice:
            size = 16
            version_address = 6

            def __init__(self):
                self.history = CommitStore()
                self.history.append(1, 0, (0,) * 16)

            def read(self, start, count):
                return (0,) * (count - 1)  # violates the read contract

        req = MirrorRequest(
            "m", 1, {"x": ShortDevice()}, {"x": GROUP}
        )
        with self.assertRaises(MultiDeviceReadError):
            MirrorScanner().snapshot_at(req)


class TestInconsistentDevice(unittest.TestCase):
    def test_silent_data_change_without_version_bump_is_rejected(self):
        # The device rewrites the data words but leaves the version at
        # the even value 2 (a device violating the version protocol).
        # The bare version gate (2 == 2, even) would accept the dirty
        # data; the content comparison against the seq-1 record must
        # reject every attempt and exhaust the budget.
        dev = make_device(
            "A", 1, script=[(), Change.set_words(0, ALL_V2)]
        )
        req = MirrorRequest("m", 1, {"A": dev}, {"A": GROUP})
        cfg = ScanConfig(max_attempts=3, max_read_words=2)
        with self.assertRaises(InconsistentMirrorScanError) as ctx:
            MirrorScanner(cfg).snapshot_at(req)
        self.assertEqual(ctx.exception.failures[0].attempts, 3)
        # The uncommitted rewrite must never have been published.
        self.assertEqual(
            [r.seq for r in dev.history.records()], [1]
        )

    def test_version_stuck_odd_exhausts_budget_no_mirror(self):
        good = make_device("good", 1)
        # After the first read the version register goes odd forever
        # (raw change, no commit -> the target record stays live).
        odd = make_device("odd", 1, script=[Change.set_version(6, 3)])
        req = MirrorRequest(
            "m", 1, {"good": good, "odd": odd},
            {"good": GROUP, "odd": GROUP},
        )
        cfg = ScanConfig(max_attempts=3, max_read_words=8)
        with self.assertRaises(InconsistentMirrorScanError) as ctx:
            MirrorScanner(cfg).snapshot_at(req)
        err = ctx.exception
        self.assertEqual(err.status, "mirror_inconsistent")
        self.assertEqual(len(err.failures), 1)
        failure = err.failures[0]
        self.assertEqual(failure.device_id, "odd")
        self.assertEqual(failure.attempts, 3)
        # Last attempt saw odd == odd (3 -> 3).
        self.assertEqual(failure.reason.version_before, 3)
        self.assertEqual(failure.reason.version_after, 3)
        # 3 attempts x (version + data + version) reads.
        self.assertEqual(odd.read_count, 9)


class TestRequestValidation(unittest.TestCase):
    def setUp(self):
        self.dev = make_device("A", 1)

    def request(self, **kw):
        params = dict(
            name="m", barrier=1, devices={"A": self.dev}, groups={"A": GROUP}
        )
        params.update(kw)
        return MirrorRequest(**params)

    def test_bad_name_barrier_and_mappings(self):
        with self.assertRaises(ConfigError):
            self.request(name="")
        with self.assertRaises(ConfigError):
            self.request(barrier=-1)
        with self.assertRaises(ConfigError):
            self.request(barrier=True)
        with self.assertRaises(ConfigError):
            self.request(devices={})
        with self.assertRaises(ConfigError):
            self.request(devices={"A": self.dev}, groups={})
        with self.assertRaises(ConfigError):
            self.request(
                devices={"A": self.dev, "B": self.dev},
                groups={"A": GROUP},
            )  # key mismatch
        with self.assertRaises(ConfigError):
            self.request(groups={"A": object()})
        with self.assertRaises(ConfigError):
            MirrorRequest("m", 1, {1: self.dev}, {1: GROUP})  # non-str id

    def test_scanner_rejects_non_request(self):
        with self.assertRaises(ConfigError):
            MirrorScanner().snapshot_at(object())

    def test_version_address_mismatch(self):
        other = RegisterGroup("g", version_address=7, fields=(ENERGY, TEMP))
        req = self.request(groups={"A": other})
        with self.assertRaises(ConfigError):
            MirrorScanner().snapshot_at(req)
        self.assertEqual(self.dev.read_count, 0)

    def test_group_outside_device_image(self):
        # Field words 15..16 extend past an image of size 16.
        big = RegisterGroup(
            "g", version_address=6,
            fields=(FieldSpec("f", 15, width=32),),
        )
        req = self.request(groups={"A": big})
        with self.assertRaises(AddressError):
            MirrorScanner().snapshot_at(req)

    def test_device_without_history(self):
        class Bare:
            size = 16
            version_address = 6

            def read(self, s, c):
                return (0,) * c

        req = MirrorRequest("m", 1, {"x": Bare()}, {"x": GROUP})
        with self.assertRaises(ConfigError):
            MirrorScanner().snapshot_at(req)


class TestIndependentCrossCheck(unittest.TestCase):
    """Small exhaustive cross-check over barriers 0..10.

    The expected seq/raw values are produced by an independent naive
    implementation: a plain scan of the raw commit tuples to pick the
    last seq <= barrier, and hand-rolled 16-bit word shifts to rebuild
    the integers.  Neither CommitStore.latest_at_or_before nor the
    package decoder is used to build expectations.
    """

    @staticmethod
    def _naive_pick(records, barrier):
        pick = None
        for rec in records:  # oldest first; last one with seq <= barrier wins
            if rec[0] <= barrier:
                pick = rec
        return pick

    @staticmethod
    def _u64_bigbig(words):  # word big, byte big
        return sum(w << (48 - 16 * i) for i, w in enumerate(words))

    @staticmethod
    def _s32_wordlittle_bytebig(words):  # low address is least significant
        raw = words[0] | (words[1] << 16)
        return raw - (1 << 32) if raw >= 1 << 31 else raw

    def test_every_barrier_matches_naive_reference(self):
        # A: commits 1 (V1), 5 (V2), 9 (V1) -- all three retained.
        a = make_device(
            "A", 1,
            script=[
                commit_v2(5),
                DeviceCommit.at(6, [Change.set_words(0, ALL_V1)], seq=9),
            ],
        )
        drain(a, 2)
        # Raw (seq, version, energy_words, temp_words) table, by hand.
        table = [
            (1, 2, ENERGY_V1, TEMP_V1),
            (5, 4, ENERGY_V2, TEMP_V2),
            (9, 6, ENERGY_V1, TEMP_V1),
        ]
        for barrier in range(0, 11):
            with self.subTest(barrier=barrier):
                req = MirrorRequest(
                    "m", barrier, {"A": a}, {"A": GROUP}
                )
                pick = self._naive_pick(table, barrier)
                if pick is None:
                    with self.assertRaises(SnapshotUnavailableError):
                        MirrorScanner().snapshot_at(req)
                    continue
                exp_seq, exp_ver, ew, tw = pick
                snap = MirrorScanner().snapshot_at(req)
                da = snap["A"]
                self.assertEqual((da.seq, da.version), (exp_seq, exp_ver))
                self.assertEqual(da["energy"].raw, self._u64_bigbig(ew))
                self.assertEqual(
                    da["temp"].raw, self._s32_wordlittle_bytebig(tw)
                )
                self.assertEqual(
                    da["temp"].value,
                    Fraction(self._s32_wordlittle_bytebig(tw), 10),
                )


class TestSnapshotShape(unittest.TestCase):
    def test_indexing_and_missing_keys(self):
        a = make_device("A", 1)
        b = make_device("B", 2)
        req = MirrorRequest(
            "m", 2, {"A": a, "B": b}, {"A": GROUP, "B": GROUP}
        )
        snap = MirrorScanner().snapshot_at(req)
        self.assertEqual(snap.name, "m")
        self.assertEqual(snap.barrier, 2)
        self.assertEqual(snap["A"].device_id, "A")
        with self.assertRaises(KeyError):
            snap["ZZ"]
        with self.assertRaises(KeyError):
            snap["A"]["nope"]


if __name__ == "__main__":
    unittest.main()
