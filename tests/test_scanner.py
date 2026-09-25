"""Scanner tests: atomicity gate, torn reads, odd versions, wraparound,
retry exhaustion, and all-or-nothing error semantics.

Expected register words are hand-computed; the scripted device schedules
are deterministic and documented inline.
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
)
from regmirror.errors import AddressError, ConfigError, DeviceReadError

# Register map used by most tests (image size 16):
#   0..3  u64 "energy"   (word big, byte big)
#   4..5  s32 "temp"     (word little, byte big), scale 1/10
#   6     version register of group "boiler"
ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec("temp", 4, width=32, signed=True, word_order="little",
                 scale=Fraction(1, 10))
GROUP = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))

# Hand-computed encodings (see tests/test_fields.py for the order model):
# energy = 0x0102030405060708 -> words 0102 0304 0506 0708
ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)
# energy = 0x0A0B0C0D0E0F1011 -> words 0A0B 0C0D 0E0F 1011
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)
# temp = 235 (raw) -> s32 0x000000EB, word little/byte big -> EB00? No:
# big-endian bytes 00 00 00 EB -> chunks (0000)(00EB); word little swaps
# the two words -> (0x00EB, 0x0000).
TEMP_V1 = (0x00EB, 0x0000)
# temp = -40 -> 0xFFFFFFD8 -> chunks (FFFF)(FFD8); word little -> (FFD8, FFFF)
TEMP_V2 = (0xFFD8, 0xFFFF)


def initial_image(version=2):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = version
    return regs


class TestCleanScan(unittest.TestCase):
    def test_commits_on_first_attempt(self):
        dev = DeviceMirror(16, initial=initial_image(version=2))
        snap = Scanner().scan(dev, [GROUP])
        g = snap["boiler"]
        self.assertEqual(g.version, 2)
        self.assertEqual(g.attempts, 1)
        self.assertEqual(g["energy"].raw, 0x0102030405060708)
        self.assertEqual(g["temp"].raw, 235)
        self.assertEqual(g["temp"].value, Fraction(47, 2))  # 235/10
        self.assertAlmostEqual(g["temp"].value_float, 23.5, places=12)

    def test_chunked_reads_used(self):
        # max_read_words=2 forces the 6-word data span into 3 chunks;
        # read pattern per attempt: version, 3 data chunks, version.
        dev = DeviceMirror(16, initial=initial_image(version=2))
        cfg = ScanConfig(max_read_words=2)
        snap = Scanner(cfg).scan(dev, [GROUP])
        self.assertEqual(snap["boiler"].attempts, 1)
        self.assertEqual(dev.read_count, 5)


class TestTornReadAcrossChunks(unittest.TestCase):
    """A 64-bit field straddling two read chunks is rewritten mid-scan.

    With max_read_words=2 the attempt reads: version(r0), chunk 0..1 (r1),
    chunk 2..3 (r2), chunk 4..5 (r3), version (r4).  The script rewrites
    the whole group (version odd, new data, version even+2) right after
    r1, so the first attempt stitches old words 0..1 onto new words 2..5
    -- a torn 64-bit value.  The version gate must reject it; the retry
    then commits the new consistent state.
    """

    def test_torn_attempt_discarded_retry_commits(self):
        # Step k applies after read k.  The change must land after r1
        # (the first data chunk), so step 0 is a no-op and step 1 is
        # the atomic rewrite of the whole group.
        dev = DeviceMirror(
            16,
            script=[
                (),  # after r0 (version read): nothing yet
                (
                    Change.set_version(6, 3),  # odd: writing
                    Change.set_words(0, ENERGY_V2 + TEMP_V2),
                    Change.set_version(6, 4),  # even: stable
                ),  # after r1: torn point
            ],
            initial=initial_image(2),
        )
        cfg = ScanConfig(max_attempts=3, max_read_words=2)
        snap = Scanner(cfg).scan(dev, [GROUP])
        g = snap["boiler"]
        self.assertEqual(g.attempts, 2)
        self.assertEqual(g.version, 4)
        # Committed values are the NEW consistent state, not the torn mix.
        self.assertEqual(g["energy"].raw, 0x0A0B0C0D0E0F1011)
        self.assertEqual(g["temp"].raw, -40)
        # The torn value the gate protected us from would have been
        # old words 0..1 + new words 2..3 = 0x010203040E0F1011.
        self.assertNotEqual(g["energy"].raw, 0x010203040E0F1011)


class TestOddVersionPersisting(unittest.TestCase):
    def test_odd_version_never_commits(self):
        # Version stays odd forever: every attempt sees v1 == v2 == 3,
        # which is odd, so nothing may ever be committed.
        dev = DeviceMirror(16, initial=initial_image(version=3))
        cfg = ScanConfig(max_attempts=5, max_read_words=2)
        with self.assertRaises(InconsistentScanError) as ctx:
            Scanner(cfg).scan(dev, [GROUP])
        err = ctx.exception
        self.assertEqual(err.attempts, 5)
        self.assertEqual(len(err.reasons), 1)
        self.assertEqual(err.reasons[0].version_before, 3)
        self.assertEqual(err.reasons[0].version_after, 3)
        # 5 attempts x (version + 3 chunks + version) reads.
        self.assertEqual(dev.read_count, 5 * 5)


class TestVersionWraparound(unittest.TestCase):
    def test_equal_even_after_wrap_commits(self):
        # Version starts at 65534 (even).  A commit between attempts
        # wraps it to 0 (65534 + 2 mod 2**16).  First attempt: v1=65534,
        # then the device writes (odd 65535, data, even 0) after the
        # version read, so v2=0 != v1 -> reject.  Retry sees 0 == 0,
        # even -> commit.  This exercises wraparound without aliasing.
        dev = DeviceMirror(
            16,
            script=[
                (
                    Change.set_version(6, 65535),
                    Change.set_words(0, ENERGY_V2 + TEMP_V2),
                    Change.set_version(6, 0),
                ),  # after r0 (first version read)
            ],
            initial=initial_image(version=65534),
        )
        cfg = ScanConfig(max_attempts=3, max_read_words=8)
        snap = Scanner(cfg).scan(dev, [GROUP])
        g = snap["boiler"]
        self.assertEqual(g.attempts, 2)
        self.assertEqual(g.version, 0)  # wrapped even version
        self.assertEqual(g["energy"].raw, 0x0A0B0C0D0E0F1011)

    def test_max_commits_contract_constant(self):
        from regmirror import MAX_COMMITS_PER_SCAN

        self.assertEqual(MAX_COMMITS_PER_SCAN, 32767)


class TestRetryExhaustion(unittest.TestCase):
    def test_budget_exhausted_raises(self):
        # Device rewrites the group after every attempt's first read
        # (the version read), so the gate fails on all 4 attempts ->
        # budget exhausted (distinct from ConfigError, which means
        # "impossible by construction").  With max_read_words=8 one
        # attempt is 3 reads: r0 version, r1 data, r2 version.  Steps
        # 0, 3, 6, 9 fire right after each attempt's r0.
        script = []
        for i in range(4):
            script.append(
                (
                    Change.set_version(6, 2 * i + 1),
                    Change.set_words(0, ENERGY_V1 + TEMP_V1),
                    Change.set_version(6, 2 * i + 2),
                )
            )
            script.extend([(), ()])  # reads r1, r2 of this attempt
        dev = DeviceMirror(16, script=script, initial=initial_image(0))
        cfg = ScanConfig(max_attempts=4, max_read_words=8)
        with self.assertRaises(InconsistentScanError) as ctx:
            Scanner(cfg).scan(dev, [GROUP])
        self.assertEqual(ctx.exception.attempts, 4)

    def test_no_partial_values_on_failure(self):
        # Two groups; the second never stabilises.  The scan must fail
        # as a whole and the exception must carry no decoded values.
        g2 = RegisterGroup("turbine", version_address=8,
                           fields=(FieldSpec("rpm", 9, width=32),))
        dev = DeviceMirror(
            16,
            script=[Change.set_version(8, 9)],  # turbine stuck odd
            initial={**initial_image(2), 8: 9, 9: 0, 10: 0},
        )
        with self.assertRaises(InconsistentScanError) as ctx:
            Scanner(ScanConfig(max_attempts=2)).scan(dev, [GROUP, g2])
        err = ctx.exception
        self.assertFalse(hasattr(err, "values"))
        self.assertFalse(hasattr(err, "snapshot"))
        for reason in err.reasons:
            self.assertEqual(reason.group, "turbine")
            self.assertIsInstance(reason.version_before, int)


class TestValidationBeforeAnyRead(unittest.TestCase):
    def test_config_errors_precede_device_access(self):
        bad_group = RegisterGroup("bad", version_address=99,
                                  fields=(FieldSpec("x", 0),))
        dev = DeviceMirror(16)
        with self.assertRaises(AddressError):
            Scanner().scan(dev, [bad_group])
        self.assertEqual(dev.read_count, 0)

    def test_overlapping_fields_accepted_and_merged(self):
        # Contract changed with the read planner: overlapping fields are
        # legal; their shared registers are merged and read once.
        g = RegisterGroup(
            "g", 10,
            fields=(FieldSpec("a", 0, width=64), FieldSpec("b", 2)),
        )
        self.assertEqual(g.required_intervals, ((0, 3),))
        dev = DeviceMirror(16, initial={0: 1, 1: 2, 2: 3, 3: 4, 10: 2})
        plan = Scanner(ScanConfig(max_read_words=4)).plan_scan(dev, [g])["g"]
        self.assertEqual(
            [(b.start, b.count) for b in plan.blocks], [(0, 4)]
        )
        snap = Scanner().scan(dev, [g])
        self.assertEqual(snap["g"]["a"].raw, 0x0001000200030004)
        self.assertEqual(snap["g"]["b"].raw, 0x00030004)

    def test_version_overlap_rejected(self):
        with self.assertRaises(ConfigError):
            RegisterGroup("g", 1, fields=(FieldSpec("a", 0),))

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ConfigError):
            RegisterGroup(
                "g", 10,
                fields=(FieldSpec("a", 0), FieldSpec("a", 2)),
            )

    def test_bool_and_range_params_rejected(self):
        with self.assertRaises(ConfigError):
            ScanConfig(max_attempts=True)
        with self.assertRaises(ConfigError):
            ScanConfig(max_attempts=0)
        with self.assertRaises(ConfigError):
            ScanConfig(max_read_words=False)
        with self.assertRaises(ConfigError):
            ScanConfig(max_read_words=0)

    def test_device_contract_checked(self):
        class NoRead:
            size = 16

        with self.assertRaises(ConfigError):
            Scanner().scan(NoRead(), [GROUP])

        class BadSize:
            size = True

            def read(self, s, c):
                return (0,) * c

        with self.assertRaises(ConfigError):
            Scanner().scan(BadSize(), [GROUP])

    def test_misbehaving_device_read_raises(self):
        class ShortRead:
            size = 16

            def read(self, s, c):
                return (0,) * (c - 1)  # lies about length

        with self.assertRaises(DeviceReadError):
            Scanner().scan(ShortRead(), [GROUP])

        class BadWord:
            size = 16

            def read(self, s, c):
                return (0,) * (c - 1) + (70000,)  # not a 16-bit word

        with self.assertRaises(DeviceReadError):
            Scanner().scan(BadWord(), [GROUP])


class TestDeviceMirrorItself(unittest.TestCase):
    def test_read_bounds_and_types(self):
        dev = DeviceMirror(4)
        with self.assertRaises(AddressError):
            dev.read(3, 2)
        with self.assertRaises(TypeError):
            dev.read(True, 1)
        with self.assertRaises(ValueError):
            dev.read(0, 0)
        with self.assertRaises(TypeError):
            DeviceMirror(4, initial={0: True})

    def test_script_applies_between_reads(self):
        dev = DeviceMirror(
            4, script=[Change.set_words(1, (0xBEEF,))], initial={1: 1}
        )
        self.assertEqual(dev.read(1, 1), (1,))       # before the change
        self.assertEqual(dev.read(1, 1), (0xBEEF,))  # after the change
        self.assertEqual(dev.read(1, 1), (0xBEEF,))  # script exhausted


if __name__ == "__main__":
    unittest.main()
