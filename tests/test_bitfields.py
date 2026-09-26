"""Bit-level field table tests: hand-computed bitstring references.

Every expected value/mask below is derived by hand from the documented
bitstring model (see regmirror/bitfields.py docstring) -- never by
calling the package's own decoder -- so these tests are an independent
reference for the cross-register, signed, endianness and union rules.
"""

import unittest

from regmirror import (
    BitFieldSpec,
    BitLayout,
    ConfigError,
    DeviceMirror,
    Change,
    InconsistentScanError,
    ScanConfig,
    Scanner,
    decode_bitfields,
)


def layout(fields, word_order="little", base=0, version=9, name="L"):
    return BitLayout(
        name,
        version_address=version,
        base_address=base,
        fields=fields,
        word_order=word_order,
    )


class TestHandComputedDecode(unittest.TestCase):
    def test_negative_spanning_three_registers_little(self):
        # little word order, base=4: logical = reg6<<32 | reg5<<16 | reg4
        # regs: reg4=0x1234, reg5=0xABCD, reg6=0x0FFF
        # logical = 0x0FFFABCD1234
        # field bits [4, 24): (logical >> 4) & 0xFFFFF
        #   logical >> 4 = 0x0FFFABCD123 -> low 20 bits = 0xCD123
        # signed 20-bit: 0xCD123 - 0x100000 = -0x32EDD = -208605
        lay = layout(
            [BitFieldSpec("temp", start_bit=4, width=20, signed=True)],
            base=4,
        )
        self.assertEqual(lay.n_words, 2)  # bits 0..23 -> 2 words
        (v,) = decode_bitfields(lay, (0x1234, 0xABCD))
        self.assertEqual(v.value, -208605)
        # bits 4..15 of word 0 -> reg4 mask 0xFFF0; bits 16..23 -> reg5 0x00FF
        self.assertEqual(v.registers, ((4, 0xFFF0), (5, 0x00FF)))

    def test_three_register_span_big_order(self):
        # big word order, base=0, n_words=3: logical = reg0<<32|reg1<<16|reg2
        # regs: reg0=0xABCD, reg1=0x1234, reg2=0xF000
        # logical = 0xABCD1234F000
        # field bits [4, 44): (logical >> 4) & (2**40 - 1)
        #   logical >> 4 = 0xABCD1234F00 -> low 40 bits = 0xBCD1234F00
        lay = layout(
            [BitFieldSpec("wide", start_bit=4, width=40)],
            word_order="big",
        )
        self.assertEqual(lay.n_words, 3)
        (v,) = decode_bitfields(lay, (0xABCD, 0x1234, 0xF000))
        self.assertEqual(v.value, 0xBCD1234F00)
        # logical word 0 (bits 4..15)  -> big: address 2, mask 0xFFF0
        # logical word 1 (bits 16..31) -> address 1, mask 0xFFFF
        # logical word 2 (bits 32..43) -> address 0, mask 0x0FFF
        self.assertEqual(
            v.registers, ((0, 0x0FFF), (1, 0xFFFF), (2, 0xFFF0))
        )

    def test_msb_exactly_aligned_signed(self):
        # big word order, 2 words; field bits [16, 32) = the whole high
        # register; its top bit (bit 31) is exactly the register MSB.
        # regs: reg0=0x8000 (MSW), reg1=0x0001
        # raw = 0x8000 -> signed 16-bit = -32768
        lay = layout(
            [BitFieldSpec("hi", start_bit=16, width=16, signed=True)],
            word_order="big",
        )
        (v,) = decode_bitfields(lay, (0x8000, 0x0001))
        self.assertEqual(v.value, -32768)
        self.assertEqual(v.registers, ((0, 0xFFFF),))

    def test_unsigned_cross_word_little(self):
        # little order: logical = reg1<<16 | reg0 = 0xABCD1234
        # field bits [8, 24): (logical >> 8) & 0xFFFF = 0xCD12
        lay = layout([BitFieldSpec("mid", start_bit=8, width=16)])
        (v,) = decode_bitfields(lay, (0x1234, 0xABCD))
        self.assertEqual(v.value, 0xCD12)
        self.assertEqual(v.registers, ((0, 0xFF00), (1, 0x00FF)))

    def test_bitstring_crosscheck(self):
        # Independent bitstring reference: build the logical bit string
        # by hand and slice it, instead of trusting the decoder's shifts.
        words = (0x1234, 0xABCD)  # little order, base 0
        logical_str = (
            format(words[1], "016b") + format(words[0], "016b")
        )[::-1]  # index i = logical bit i
        self.assertEqual(logical_str[:16], format(0x1234, "016b")[::-1])
        field_bits = logical_str[8:24][::-1]  # bits [8,24), MSB first
        self.assertEqual(int(field_bits, 2), 0xCD12)
        lay = layout([BitFieldSpec("mid", start_bit=8, width=16)])
        (v,) = decode_bitfields(lay, words)
        self.assertEqual(v.value, int(field_bits, 2))


class TestUnionAndOverlap(unittest.TestCase):
    def test_union_views_may_overlap(self):
        lay = layout(
            [
                BitFieldSpec("u16", 0, 16, union="reg0"),
                BitFieldSpec("hi_s8", 8, 8, signed=True, union="reg0"),
                BitFieldSpec("flag0", 0, 1, union="reg0"),
            ]
        )
        values = {v.name: v for v in decode_bitfields(lay, (0x80FF,))}
        self.assertEqual(values["u16"].value, 0x80FF)
        self.assertEqual(values["hi_s8"].value, -128)  # 0x80 as s8
        self.assertEqual(values["flag0"].value, 1)

    def test_overlap_without_union_rejected(self):
        with self.assertRaises(ConfigError):
            layout(
                [
                    BitFieldSpec("a", 0, 16),
                    BitFieldSpec("b", 8, 8),
                ]
            )

    def test_overlap_across_different_unions_rejected(self):
        with self.assertRaises(ConfigError):
            layout(
                [
                    BitFieldSpec("a", 0, 16, union="u1"),
                    BitFieldSpec("b", 8, 8, union="u2"),
                ]
            )

    def test_adjacent_fields_are_legal(self):
        lay = layout(
            [BitFieldSpec("a", 0, 8), BitFieldSpec("b", 8, 8)]
        )
        a, b = decode_bitfields(lay, (0xBEEF,))
        self.assertEqual((a.value, b.value), (0xEF, 0xBE))

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ConfigError):
            layout([BitFieldSpec("x", 0, 1), BitFieldSpec("x", 1, 1)])

    def test_version_register_overlap_rejected(self):
        with self.assertRaises(ConfigError):
            layout([BitFieldSpec("a", 0, 16)], version=0)

    def test_bad_field_params_rejected(self):
        with self.assertRaises(ConfigError):
            BitFieldSpec("a", -1, 4)
        with self.assertRaises(ConfigError):
            BitFieldSpec("a", 0, 0)
        with self.assertRaises(ConfigError):
            BitFieldSpec("a", 0, True)  # bool width rejected
        with self.assertRaises(ConfigError):
            BitFieldSpec("a", 0, 4, signed=1)


class TestDecodeInputValidation(unittest.TestCase):
    def test_missing_words_rejected_no_partial(self):
        lay = layout(
            [BitFieldSpec("a", 0, 16), BitFieldSpec("b", 16, 16)]
        )
        with self.assertRaises(ConfigError):
            decode_bitfields(lay, (0x0001,))  # one word short

    def test_extra_words_rejected(self):
        lay = layout([BitFieldSpec("a", 0, 16)])
        with self.assertRaises(ConfigError):
            decode_bitfields(lay, (0x0001, 0x0002))

    def test_non_16bit_word_rejected(self):
        lay = layout([BitFieldSpec("a", 0, 16)])
        for bad in ((0x10000,), (-1,), (True,), ("x",)):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    decode_bitfields(lay, bad)


class TestScanLayout(unittest.TestCase):
    def test_commit_from_single_verified_mirror(self):
        lay = layout(
            [
                BitFieldSpec("temp", 4, 20, signed=True),
                BitFieldSpec("mode", 24, 4),
            ],
            base=4,
            version=2,
        )
        # logical (little, base 4) = reg5<<16 | reg4
        # temp bits [4,24) of 0xABCD1234 -> 0xCD123 -> -208605 (as above)
        # mode bits [24,28) of logical... n_words=2 covers bits 0..31;
        # logical = 0xABCD1234, bits 24..27 = 0xB
        dev = DeviceMirror(
            8, initial={2: 10, 4: 0x1234, 5: 0xABCD}
        )
        snap = Scanner().scan_layout(dev, lay)
        self.assertEqual(snap.version, 10)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual(snap["temp"].value, -208605)
        self.assertEqual(snap["temp"].registers, ((4, 0xFFF0), (5, 0x00FF)))
        self.assertEqual(snap["mode"].value, 0xB)
        self.assertEqual(snap["mode"].registers, ((5, 0x0F00),))

    def test_retry_after_torn_read_then_commit(self):
        lay = layout([BitFieldSpec("a", 0, 16)], base=0, version=5)
        # Read order per attempt: version, data, version.
        # Attempt 1: v1=4, then device bumps version to 6 -> v2=6 (torn).
        # Attempt 2: stable at 6 -> commit.
        dev = DeviceMirror(
            8,
            script=[
                (),  # after read 1 (v1)
                Change.set_version(5, 6),  # after read 2 (data)
            ],
            initial={5: 4, 0: 0x00FE},
        )
        snap = Scanner().scan_layout(dev, lay)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.version, 6)
        self.assertEqual(snap["a"].value, 0x00FE)

    def test_generation_conflict_exhausts_budget_no_partial(self):
        lay = layout([BitFieldSpec("a", 0, 16)], base=0, version=5)
        # Version stays odd forever: every attempt fails the gate.
        dev = DeviceMirror(8, initial={5: 7, 0: 0x00FE})
        scanner = Scanner(ScanConfig(max_attempts=3))
        with self.assertRaises(InconsistentScanError) as ctx:
            scanner.scan_layout(dev, lay)
        err = ctx.exception
        self.assertEqual(err.attempts, 3)
        # The error carries only version-word diagnostics, never values.
        self.assertFalse(hasattr(err, "values"))
        for reason in err.reasons:
            self.assertEqual(reason.group, lay.name)

    def test_layout_validated_before_any_device_read(self):
        dev = DeviceMirror(8, initial={5: 4})
        bad = object()  # not a BitLayout
        with self.assertRaises(ConfigError):
            Scanner().scan_layout(dev, bad)
        self.assertEqual(dev.read_count, 0)
        # Out-of-image layout: also rejected before the first read.
        lay = layout([BitFieldSpec("a", 0, 16)], base=8, version=5)
        with self.assertRaises(ConfigError):
            Scanner().scan_layout(dev, lay)
        self.assertEqual(dev.read_count, 0)


if __name__ == "__main__":
    unittest.main()
