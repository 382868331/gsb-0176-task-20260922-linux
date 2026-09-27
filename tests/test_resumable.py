"""Resumable scan and publisher tests.

Covers: pause/resume without changes, device change during a pause
(stale group discarded and rescanned), the final revalidation pass,
rescan budget exhaustion, and the publisher keeping the previous
complete snapshot when the retry budget is exhausted.

All device changes are driven by deterministic DeviceMirror scripts;
read indices are documented inline (step k lands after the k-th read).
"""

import unittest
from fractions import Fraction

from regmirror import (
    Change,
    DeviceMirror,
    FieldSpec,
    InconsistentScanError,
    RegisterGroup,
    ScanConfig,
    Scanner,
    Snapshot,
    SnapshotPublisher,
)
from regmirror.errors import ConfigError
from regmirror.resumable import ScanSession

# Register map (image size 16):
#   0..3  u64 "energy" (word big, byte big)      group "boiler"
#   4..5  s32 "temp" (word little, byte big)     group "boiler"
#   6     version register of "boiler"
#   8..9  u32 "flow" (word big, byte big)        group "pump"
#   10    version register of "pump"
ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec("temp", 4, width=32, signed=True, word_order="little",
                 scale=Fraction(1, 10))
BOILER = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))
FLOW = FieldSpec("flow", 8, width=32)
PUMP = RegisterGroup("pump", version_address=10, fields=(FLOW,))
GROUPS = [BOILER, PUMP]

# Hand-computed encodings (same model as tests/test_scanner.py).
ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)  # 0x0102030405060708
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)  # 0x0A0B0C0D0E0F1011
TEMP_V1 = (0x00EB, 0x0000)                    # raw 235
TEMP_V2 = (0xFFD8, 0xFFFF)                    # raw -40
FLOW_V1 = (0x0000, 0x0064)                    # raw 100

SCANNER = Scanner(ScanConfig(max_attempts=4, max_read_words=16))


def image(boiler_version=2, pump_version=2):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = boiler_version
    regs[8] = FLOW_V1[0]
    regs[9] = FLOW_V1[1]
    regs[10] = pump_version
    return regs


def boiler_rewrite(new_version):
    """Atomic rewrite of the whole boiler group + version bump."""
    return (
        Change.set_version(6, new_version - 1),  # odd: write in progress
        Change.set_words(0, ENERGY_V2 + TEMP_V2),
        Change.set_version(6, new_version),
    )


class TestPauseResume(unittest.TestCase):
    def test_pause_without_change_matches_one_shot(self):
        dev = DeviceMirror(16, initial=image())
        expected = SCANNER.scan(DeviceMirror(16, initial=image()), GROUPS)
        session = ScanSession(SCANNER, dev, GROUPS)
        steps = 0
        while not session.step():  # pause between every step
            steps += 1
        snap = session.run()
        self.assertEqual(snap, expected)
        self.assertEqual(snap["boiler"]["energy"].raw, 0x0102030405060708)
        self.assertEqual(snap["pump"]["flow"].raw, 100)

    def test_change_during_pause_discards_committed_group(self):
        # Reads: step1 scans boiler (reads 0..2, commits version 2).
        # Script step 2 lands right after read 2, i.e. "during the pause",
        # rewriting boiler and bumping its version 2 -> 4.
        dev = DeviceMirror(16, script=[(), (), boiler_rewrite(4)],
                           initial=image())
        session = ScanSession(SCANNER, dev, GROUPS)
        self.assertFalse(session.step())          # boiler committed @ v2
        self.assertEqual(session.committed_names, ("boiler",))
        # "pause" happens here; the scripted device change is already
        # scheduled and is only observed on the next step.
        self.assertFalse(session.step())          # revalidate + scan pump
        # boiler was detected stale and dropped back to pending:
        self.assertEqual(session.committed_names, ("pump",))
        self.assertEqual(session.pending_names, ("boiler",))
        snap = session.run()
        # Rescanned boiler carries the post-pause version and data.
        self.assertEqual(snap["boiler"].version, 4)
        self.assertEqual(snap["boiler"]["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(snap["boiler"]["temp"].raw, -40)
        self.assertEqual(snap["pump"]["flow"].raw, 100)

    def test_final_revalidation_catches_late_change(self):
        # step1: boiler scan (reads 0..2). step2: revalidate (read 3),
        # pump scan (reads 4..6). Script step 6 lands after read 6,
        # rewriting boiler *after* its commit but before completion.
        dev = DeviceMirror(
            16, script=[()] * 6 + [boiler_rewrite(4)], initial=image()
        )
        snap = ScanSession(SCANNER, dev, GROUPS).run()
        self.assertEqual(snap["boiler"].version, 4)
        self.assertEqual(snap["boiler"]["energy"].raw, 0x0A0B0C0D0E0F1011)

    def test_rescan_budget_exhausted(self):
        # max_attempts=1: one group may be scanned only once per session.
        # Script step 2 (after the first scan) rewrites boiler, so the
        # revalidation on step 2 goes stale and the rescan is refused.
        scanner = Scanner(ScanConfig(max_attempts=1, max_read_words=16))
        dev = DeviceMirror(16, script=[(), (), boiler_rewrite(4)],
                           initial=image())
        session = ScanSession(scanner, dev, [BOILER])
        session.step()
        with self.assertRaises(InconsistentScanError) as ctx:
            session.step()
        self.assertEqual(ctx.exception.reasons[0].group, "boiler")
        self.assertEqual(ctx.exception.reasons[0].version_before, 2)
        self.assertEqual(ctx.exception.reasons[0].version_after, 4)

    def test_run_raises_when_device_never_stable(self):
        dev = DeviceMirror(16, initial=image(boiler_version=3))  # odd
        with self.assertRaises(InconsistentScanError):
            ScanSession(SCANNER, dev, GROUPS).run()

    def test_config_validated_before_any_read(self):
        dev = DeviceMirror(16, initial=image())
        with self.assertRaises(ConfigError):
            ScanSession(SCANNER, dev, [BOILER, BOILER])
        self.assertEqual(dev.read_count, 0)


class TestPublisher(unittest.TestCase):
    def test_update_publishes_complete_snapshot(self):
        pub = SnapshotPublisher(SCANNER, DeviceMirror(16, initial=image()),
                                GROUPS)
        self.assertIsNone(pub.snapshot)
        result = pub.update()
        self.assertTrue(result.updated)
        self.assertIsNone(result.error)
        self.assertIs(result.snapshot, pub.snapshot)
        self.assertEqual(len(result.snapshot), 2)

    def test_exhausted_retries_keep_previous_snapshot(self):
        # First update succeeds with 9 reads (see test_pause... layout).
        # Script step 9 makes the boiler version odd forever after that,
        # so the second update exhausts its retry budget.
        script = [()] * 9 + [Change.set_version(6, 3)]
        pub = SnapshotPublisher(
            SCANNER, DeviceMirror(16, script=script, initial=image()), GROUPS
        )
        first = pub.update()
        self.assertTrue(first.updated)
        second = pub.update()
        self.assertFalse(second.updated)
        self.assertIsInstance(second.error, InconsistentScanError)
        # The published image is still the previous complete snapshot,
        # not a mix of old and new versions.
        self.assertIs(second.snapshot, first.snapshot)
        self.assertIs(pub.snapshot, first.snapshot)

    def test_first_update_failure_publishes_nothing(self):
        dev = DeviceMirror(16, initial=image(boiler_version=3))  # odd
        pub = SnapshotPublisher(SCANNER, dev, GROUPS)
        result = pub.update()
        self.assertFalse(result.updated)
        self.assertIsNone(result.snapshot)
        self.assertIsNone(pub.snapshot)
        self.assertIsInstance(result.error, InconsistentScanError)

    def test_publish_rejects_partial_snapshot(self):
        pub = SnapshotPublisher(SCANNER, DeviceMirror(16, initial=image()),
                                GROUPS)
        only_boiler = Snapshot(
            (SCANNER.scan_group(DeviceMirror(16, initial=image()), BOILER),)
        )
        with self.assertRaises(ConfigError):
            pub.publish(only_boiler)
        self.assertIsNone(pub.snapshot)

    def test_driven_session_can_be_published(self):
        pub = SnapshotPublisher(SCANNER, DeviceMirror(16, initial=image()),
                                GROUPS)
        session = pub.new_session()
        while not session.step():  # caller pauses between steps
            pass
        result = pub.publish(session.run())
        self.assertTrue(result.updated)
        self.assertEqual(pub.snapshot["pump"]["flow"].raw, 100)


if __name__ == "__main__":
    unittest.main()

