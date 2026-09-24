"""Tests for versioned commit history and the history-keeping synthetic
device: append/retention invariants, non-contiguous global seqs, and the
deterministic read-bound commit schedule.

Expected words/seqs are hand-computed; CommitStore is used directly only
as the object under test.
"""

import unittest
from fractions import Fraction

from regmirror import (
    Change,
    CommitRecord,
    CommitStore,
    DeviceCommit,
    DeviceMirror,
    FieldSpec,
    HistoricalDevice,
    RegisterGroup,
)
from regmirror.errors import AddressError, DeviceReadError

ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec(
    "temp", 4, width=32, signed=True, word_order="little",
    scale=Fraction(1, 10),
)
GROUP = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))

ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)
TEMP_V1 = (0x00EB, 0x0000)
TEMP_V2 = (0xFFD8, 0xFFFF)


def initial_image():
    # Data words only; the version word is supplied via initial_version.
    return dict(enumerate(ENERGY_V1 + TEMP_V1))


class TestCommitStore(unittest.TestCase):
    def test_append_and_latest_at_or_before(self):
        store = CommitStore()
        self.assertIsNone(store.oldest)
        self.assertIsNone(store.newest)
        self.assertIsNone(store.latest_at_or_before(10))
        store.append(1, 0, (0x1111,))
        store.append(5, 2, (0x2222,))   # non-contiguous global seqs
        store.append(10, 4, (0x3333,))
        self.assertEqual(store.oldest_seq, 1)
        self.assertEqual(store.newest.seq, 10)
        # Barrier semantics: last commit with seq <= barrier.
        self.assertEqual(store.latest_at_or_before(0), None)
        self.assertEqual(store.latest_at_or_before(1).seq, 1)
        self.assertEqual(store.latest_at_or_before(4).seq, 1)   # gap
        self.assertEqual(store.latest_at_or_before(5).seq, 5)
        self.assertEqual(store.latest_at_or_before(9).seq, 5)
        self.assertEqual(store.latest_at_or_before(10).seq, 10)
        self.assertEqual(store.latest_at_or_before(1_000_000).seq, 10)

    def test_records_are_full_immutable_copies(self):
        words = [1, 2, 3]
        store = CommitStore()
        rec = store.append(1, 0, words)
        words[0] = 999  # mutating the caller's list must not change history
        self.assertEqual(rec.words, (1, 2, 3))
        self.assertEqual(store.records()[0].words, (1, 2, 3))
        with self.assertRaises(Exception):
            rec.version = 6  # frozen

    def test_bounded_retention_drops_oldest(self):
        store = CommitStore(max_records=2)
        store.append(1, 0, (1,))
        store.append(5, 2, (2,))
        store.append(9, 4, (3,))
        self.assertEqual(len(store), 2)
        self.assertEqual(store.oldest_seq, 5)
        self.assertEqual(
            [r.seq for r in store.records()], [5, 9]
        )
        # A dropped commit is indistinguishable from "too old".
        self.assertIsNone(store.latest_at_or_before(1))
        self.assertEqual(store.latest_at_or_before(5).seq, 5)

    def test_rejects_bad_append(self):
        store = CommitStore()
        store.append(3, 0, (1,))
        with self.assertRaises(ValueError):
            store.append(3, 2, (1,))   # seq must strictly increase
        with self.assertRaises(ValueError):
            store.append(4, 1, (1,))   # odd version is not a stable commit
        with self.assertRaises(ValueError):
            store.append(4, 2, (1, 70000))  # not a 16-bit word
        with self.assertRaises(TypeError):
            CommitStore(max_records=True)
        with self.assertRaises(ValueError):
            CommitStore(max_records=0)

    def test_barrier_bool_rejected(self):
        with self.assertRaises(TypeError):
            CommitStore().latest_at_or_before(True)


class TestHistoricalDeviceConstruction(unittest.TestCase):
    def test_initial_commit_recorded(self):
        dev = HistoricalDevice(
            "d1", 16, 6, initial_version=2, initial=initial_image(),
            start_seq=1,
        )
        rec = dev.history.newest
        self.assertEqual((rec.seq, rec.version), (1, 2))
        # Full image, including the version word and every field.
        self.assertEqual(rec.words[6], 2)
        self.assertEqual(rec.words[0:6], ENERGY_V1 + TEMP_V1)

    def test_bad_construction_inputs(self):
        with self.assertRaises(TypeError):
            HistoricalDevice("", 16, 6)
        with self.assertRaises(ValueError):
            HistoricalDevice("d", 16, 6, initial_version=3)  # odd
        with self.assertRaises(ValueError):
            HistoricalDevice("d", 16, 6, start_seq=0)
        with self.assertRaises(ValueError):
            HistoricalDevice("d", 16, 16)  # version address out of image
        with self.assertRaises(ValueError):
            HistoricalDevice(
                "d", 16, 6, initial={6: 2}
            )  # version word via initial is ambiguous

    def test_is_a_device_mirror(self):
        dev = HistoricalDevice("d", 16, 6, initial=initial_image())
        self.assertIsInstance(dev, DeviceMirror)
        self.assertEqual(dev.size, 16)
        self.assertEqual(dev.version_address, 6)
        with self.assertRaises(AddressError):
            dev.read(15, 2)


class TestHistoricalDeviceCommits(unittest.TestCase):
    def test_commit_step_applies_between_reads_and_records(self):
        # Read #0 sees the initial state; the commit lands after it;
        # read #1 sees the new stable state and its history entry exists.
        dev = HistoricalDevice(
            "d1", 16, 6, initial_version=2, initial=initial_image(),
            start_seq=1,
            script=[
                DeviceCommit.at(
                    4, [Change.set_words(0, ENERGY_V2 + TEMP_V2)], seq=8
                ),
            ],
        )
        # Before any read the history holds only the initial commit.
        self.assertEqual([r.seq for r in dev.history.records()], [1])
        self.assertEqual(dev.read(0, 1)[0], ENERGY_V1[0])
        # Step 0 landed after read #0: read #1 sees the new state.
        self.assertEqual(dev.read(0, 1)[0], ENERGY_V2[0])
        self.assertEqual([r.seq for r in dev.history.records()], [1, 8])
        rec = dev.history.newest
        self.assertEqual((rec.seq, rec.version), (8, 4))
        self.assertEqual(rec.words[0:6], ENERGY_V2 + TEMP_V2)
        # The old record is untouched and still fully decodable.
        old = dev.history.records()[0]
        self.assertEqual(old.words[0:6], ENERGY_V1 + TEMP_V1)

    def test_commit_auto_seq(self):
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0, start_seq=10,
            script=[DeviceCommit.at(2)],  # no seq -> next sequential
        )
        dev.read(0, 1)
        self.assertEqual(dev.history.newest.seq, 11)

    def test_commit_may_not_write_version_register(self):
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0,
            script=[DeviceCommit.at(2, [Change.set_words(6, (9,))])],
        )
        with self.assertRaises(ValueError):
            dev.read(0, 1)

    def test_bad_commit_leaves_no_half_update(self):
        # Two changes in one commit step; the second writes outside the
        # image.  Nothing may be applied and no record appended.
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0, initial=initial_image(),
            script=[
                DeviceCommit.at(
                    2,
                    [
                        Change.set_words(0, ENERGY_V2 + TEMP_V2),
                        Change.set_words(15, (1, 2)),  # 15..16 out of image
                    ],
                ),
            ],
        )
        with self.assertRaises(ValueError):
            dev.read(0, 1)
        # Image is still the initial V1 state; history still has seq 1.
        self.assertEqual(dev.peek(0, 6), ENERGY_V1 + TEMP_V1)
        self.assertEqual([r.seq for r in dev.history.records()], [1])
        self.assertEqual(dev.peek(6, 1), (0,))

    def test_bounded_device_history(self):
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0, max_records=2,
            script=[DeviceCommit.at(2), DeviceCommit.at(4)],
        )
        dev.read(0, 1)
        dev.read(0, 1)
        self.assertEqual([r.seq for r in dev.history.records()], [2, 3])

    def test_plain_changes_without_commit_do_not_record(self):
        # A raw set_version is allowed (e.g. stuck-odd fault injection)
        # but appends no history.
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0,
            script=[Change.set_version(6, 3)],
        )
        dev.read(0, 1)
        self.assertEqual(len(dev.history), 1)
        self.assertEqual(dev.peek(6, 1), (3,))

    def test_faulted_read_raises_but_schedule_advances(self):
        fault = DeviceReadError("simulated bus fault")
        dev = HistoricalDevice(
            "d", 16, 6, initial_version=0,
            script=[DeviceCommit.at(2)],
            faults={0: fault},
        )
        with self.assertRaises(DeviceReadError):
            dev.read(0, 1)
        # The commit scheduled after read #0 still applied: the device
        # kept moving even though the read response was lost.
        self.assertEqual(dev.history.newest.version, 2)
        self.assertEqual(dev.read(6, 1), (2,))


if __name__ == "__main__":
    unittest.main()
