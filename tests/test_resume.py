"""Pause/resume and atomic publication tests."""

import unittest

from regmirror import (
    Change,
    DeviceMirror,
    FieldSpec,
    PublishedImage,
    RegisterGroup,
    ScanConfig,
    Scanner,
)


ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec(
    "temp", 4, width=32, signed=True, word_order="little"
)
GROUP = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))
TURBINE = RegisterGroup(
    "turbine", version_address=8, fields=(FieldSpec("rpm", 9),)
)

ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)
TEMP_V1 = (0x00EB, 0x0000)
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)
TEMP_V2 = (0xFFD8, 0xFFFF)


def initial(version=2, turbine_version=2):
    words = dict(enumerate(ENERGY_V1 + TEMP_V1))
    words[6] = version
    words[8] = turbine_version
    words[9] = 0
    words[10] = 0
    return words


class PauseAfterRead:
    def __init__(self, device, session, read_index):
        self._device = device
        self._session = session
        self._read_index = read_index
        self.size = device.size

    def read(self, start, count):
        words = self._device.read(start, count)
        if self._session is not None and self._device.read_count == self._read_index:
            self._session.request_pause()
        return words


class TestPauseResume(unittest.TestCase):
    def test_resume_without_device_change_completes(self):
        device = DeviceMirror(16, initial=initial())
        scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=2))
        proxied = PauseAfterRead(device, None, read_index=2)
        session = scanner.start_session(proxied, [GROUP])
        proxied._session = session

        self.assertIsNone(session.run())
        self.assertEqual(session.status, "paused")
        self.assertEqual(device.read_count, 2)

        snapshot = session.run()
        self.assertEqual(session.status, "complete")
        self.assertEqual(snapshot["boiler"].version, 2)
        self.assertEqual(snapshot["boiler"].attempts, 1)
        self.assertEqual(snapshot["boiler"]["energy"].raw, 0x0102030405060708)

    def test_change_during_pause_invalidates_buffered_words(self):
        device = DeviceMirror(
            16,
            script=[
                (),
                (
                    Change.set_version(6, 3),
                    Change.set_words(0, ENERGY_V2 + TEMP_V2),
                    Change.set_version(6, 4),
                ),
            ],
            initial=initial(),
        )
        scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=2))
        proxied = PauseAfterRead(device, None, read_index=2)
        session = scanner.start_session(proxied, [GROUP])
        proxied._session = session

        self.assertIsNone(session.run())
        snapshot = session.run()

        group = snapshot["boiler"]
        self.assertEqual(group.attempts, 2)
        self.assertEqual(group.version, 4)
        self.assertEqual(group["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(group["temp"].raw, -40)

    def test_completed_group_changed_during_pause_restarts_whole_image(self):
        device = DeviceMirror(
            16,
            initial=initial(),
        )
        scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=8))
        proxied = PauseAfterRead(device, None, read_index=3)
        session = scanner.start_session(proxied, [GROUP, TURBINE])
        proxied._session = session

        self.assertIsNone(session.run())
        self.assertEqual(device.read_count, 3)
        device.apply_changes(
            [
                Change.set_version(6, 4),
                Change.set_words(0, ENERGY_V2 + TEMP_V2),
            ]
        )
        snapshot = session.run()

        self.assertEqual(snapshot["boiler"].version, 4)
        self.assertEqual(snapshot["boiler"].attempts, 2)
        self.assertEqual(snapshot["boiler"]["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(snapshot["boiler"]["temp"].raw, -40)
        self.assertEqual(snapshot["turbine"].version, 2)
        self.assertEqual(device.read_count, 10)


class TestPublishedImage(unittest.TestCase):
    def test_failed_update_keeps_previous_complete_image(self):
        scanner = Scanner(ScanConfig(max_attempts=2))
        publisher = PublishedImage()
        good_device = DeviceMirror(16, initial=initial(version=2, turbine_version=2))

        first = publisher.try_update(scanner, good_device, [GROUP, TURBINE])
        self.assertTrue(first.updated)
        self.assertIs(first.snapshot, publisher.current)

        bad_device = DeviceMirror(
            16, initial=initial(version=3, turbine_version=3)
        )
        second = publisher.try_update(scanner, bad_device, [GROUP, TURBINE])

        self.assertFalse(second.updated)
        self.assertIs(second.snapshot, first.snapshot)
        self.assertIs(publisher.current, first.snapshot)
        self.assertIs(second.error, publisher.last_error)
        self.assertEqual(publisher.current["boiler"].version, 2)


if __name__ == "__main__":
    unittest.main()
