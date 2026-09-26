"""Bit-field table tests: cross-register fields, unions, masks.

Every expected bit string, mask and value below is computed by hand
from the documented bit model -- never by calling the package itself --
so these tests are an independent reference.
"""

import unittest

from regmirror import (
    BitFieldGroup,
    BitFieldSpec,
    BitFieldTable,
    Change,
    DeviceMirror,
    IncompleteImageError,
    ScanConfig,
    Scanner,
    decode_bitfields,
    register_masks,
)
from regmirror.errors import (
    ConfigError,
    DeviceReadError,
    InconsistentScanError,
)


class TestHandComputedBitStrings(unittest.TestCase):
    """Hand-derived bit-string references.

    Reference layout (big word order), field bits 4..39 (offset 4,
    width 36), value -2 as 36-bit two's complement:

        raw_u = 0xFFFFFFFFE  (36 bits, all ones except bit 0)
        logical = raw_u << 4 = 0xFFFFFFFFE0
        reg0 (logical bits 39..32) = 0x00FF
        reg1 (logical bits 31..16) = 0xFFFF
        reg2 (logical bits 15..0)  = 0xFFE0
    """

    def test_negative_spanning_three_registers_big_order(self):
        table = BitFieldTable(
            0, [BitFieldSpec("temp", 4, 36, signed=True, word_order="big")]
        )
        (v,) = decode_bitfields(table, {0: 0x00FF, 1: 0xFFFF, 2: 0xFFE0})
        self.assertEqual(v.raw, -2)
        self.assertEqual(v.unsigned, 0xFFFFFFFFE)
        self.assertEqual(v.registers, (0, 1, 2))
        self.assertEqual(v.masks, (0x00FF, 0xFFFF, 0xFFF0))

    def test_negative_spanning_three_registers_little_order(self):
        # Same logical bits, little word order: the lowest-address
        # register holds the least significant bits, so the register
        # image is mirrored relative to the big-order case.
        table = BitFieldTable(
            0, [BitFieldSpec("temp", 4, 36, signed=True, word_order="little")]
        )
        (v,) = decode_bitfields(table, {0: 0xFFE0, 1: 0xFFFF, 2: 0x00FF})
        self.assertEqual(v.raw, -2)
        self.assertEqual(v.unsigned, 0xFFFFFFFFE)
        self.assertEqual(v.registers, (0, 1, 2))
        self.assertEqual(v.masks, (0xFFF0, 0xFFFF, 0x00FF))

    def test_msb_exactly_aligned(self):
        # offset 12, width 20: field MSB is logical bit 31 == bit 15 of
        # the high register -- the field top aligns exactly with the
        # register top.  Value -1 -> all 20 field bits set.
        # logical = 0xFFFFF << 12 = 0xFFFFF000 -> reg0 = 0xFFFF,
        # reg1 = 0xF000 (big word order).
        table = BitFieldTable(
            0, [BitFieldSpec("align", 12, 20, signed=True, word_order="big")]
        )
        (v,) = decode_bitfields(table, {0: 0xFFFF, 1: 0xF000})
        self.assertEqual(v.raw, -1)
        self.assertEqual(v.unsigned, 0xFFFFF)
        self.assertEqual(v.registers, (0, 1))
        self.assertEqual(v.masks, (0xFFFF, 0xF000))

    def test_msb_aligned_unsigned_single_register(self):
        # offset 0, width 16: MSB of the field is bit 15 of the
        # register.  0x8000 -> 32768 unsigned / -32768 signed.
        unsigned = BitFieldTable(5, [BitFieldSpec("u", 0, 16)])
        signed = BitFieldTable(5, [BitFieldSpec("s", 0, 16, signed=True)])
        self.assertEqual(decode_bitfields(unsigned, {5: 0x8000})[0].raw, 32768)
        self.assertEqual(decode_bitfields(signed, {5: 0x8000})[0].raw, -32768)

    def test_single_bit_field(self):
        table = BitFieldTable(0, [BitFieldSpec("flag", 3, 1)])
        (v,) = decode_bitfields(table, {0: 0x0008})
        self.assertEqual(v.raw, 1)
        self.assertEqual(v.registers, (0,))
        self.assertEqual(v.masks, (0x0008,))
        (v0,) = decode_bitfields(table, {0: 0xFFF7})
        self.assertEqual(v0.raw, 0)


class TestLayoutValidation(unittest.TestCase):
    def test_overlap_rejected_before_any_scan(self):
        with self.assertRaises(ConfigError):
            BitFieldTable(
                0,
                [
                    BitFieldSpec("a", 0, 16),
                    BitFieldSpec("b", 8, 16),  # shares bits 8..15 of reg 0
                ],
            )

    def test_same_register_disjoint_masks_allowed(self):
        table = BitFieldTable(
            0,
            [
                BitFieldSpec("lo", 0, 8),
                BitFieldSpec("hi", 8, 8),
            ],
        )
        values = decode_bitfields(table, {0: 0xABCD})
        self.assertEqual([v.raw for v in values], [0xCD, 0xAB])

    def test_union_views_allowed(self):
        table = BitFieldTable(
            0,
            [
                BitFieldSpec("as_unsigned", 0, 16, union="u1"),
                BitFieldSpec("as_signed", 0, 16, signed=True, union="u1"),
            ],
        )
        values = decode_bitfields(table, {0: 0xFFFF})
        self.assertEqual([v.raw for v in values], [0xFFFF, -1])
        for v in values:
            self.assertEqual(v.registers, (0,))
            self.assertEqual(v.masks, (0xFFFF,))

    def test_different_union_tags_still_rejected(self):
        with self.assertRaises(ConfigError):
            BitFieldTable(
                0,
                [
                    BitFieldSpec("a", 0, 16, union="u1"),
                    BitFieldSpec("b", 0, 16, union="u2"),
                ],
            )

    def test_union_tag_must_be_declared_on_both_sides(self):
        with self.assertRaises(ConfigError):
            BitFieldTable(
                0,
                [
                    BitFieldSpec("a", 0, 16, union="u1"),
                    BitFieldSpec("b", 0, 16),
                ],
            )

    def test_invalid_specs_rejected(self):
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", 0, 0)  # zero width
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", 0, 65)  # too wide
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", -1, 8)  # negative offset
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", True, 8)  # bool offset
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", 0, 8, word_order="middle")
        with self.assertRaises(ConfigError):
            BitFieldSpec("x", 0, 8, union="")  # empty tag
        with self.assertRaises(ConfigError):
            BitFieldTable(
                0, [BitFieldSpec("a", 0, 8), BitFieldSpec("a", 16, 8)]
            )

    def test_table_span_and_register_masks(self):
        spec = BitFieldSpec("t", 4, 36, word_order="big")
        self.assertEqual(
            register_masks(spec, 10),
            ((10, 0x00FF), (11, 0xFFFF), (12, 0xFFF0)),
        )
        # "u" uses global bits 32..35 -> register 12 bits 0..3 (big
        # order), exactly the nibble "t" leaves free in that register.
        table = BitFieldTable(10, [spec, BitFieldSpec("u", 32, 4)])
        self.assertEqual(table.register_span, (10, 12))
        self.assertEqual(
            table.registers_for("t"),
            ((10, 0x00FF), (11, 0xFFFF), (12, 0xFFF0)),
        )
        self.assertEqual(table.registers_for("u"), ((12, 0x000F),))


class TestAtomicDecode(unittest.TestCase):
    def setUp(self):
        self.table = BitFieldTable(
            0,
            [
                BitFieldSpec("a", 0, 16),
                BitFieldSpec("b", 16, 16),
                BitFieldSpec("c", 32, 4),
            ],
        )

    def test_missing_word_produces_no_partial_result(self):
        with self.assertRaises(IncompleteImageError) as ctx:
            decode_bitfields(self.table, {0: 1, 1: 2})  # register 2 missing
        self.assertIn("2", str(ctx.exception))

    def test_malformed_word_rejected(self):
        with self.assertRaises(DeviceReadError):
            decode_bitfields(self.table, {0: 1, 1: 2, 2: 0x1_0000})
        with self.assertRaises(DeviceReadError):
            decode_bitfields(self.table, {0: 1, 1: True, 2: 3})

    def test_bad_arguments_rejected(self):
        with self.assertRaises(ConfigError):
            decode_bitfields("not a table", {})
        with self.assertRaises(ConfigError):
            decode_bitfields(self.table, [0, 1, 2])


class TestScannerIntegration(unittest.TestCase):
    """BitFieldGroup plugs into the version-gated scanner: one verified
    mirror, one decode; generation conflicts yield no values at all."""

    def _table(self):
        return BitFieldTable(
            0,
            [
                BitFieldSpec("raw16", 0, 16, union="view"),
                BitFieldSpec("signed16", 0, 16, signed=True, union="view"),
                BitFieldSpec("wide", 20, 36, signed=True, word_order="big"),
            ],
        )

    def test_scan_decodes_two_union_views_of_one_mirror(self):
        # wide: offset 20, width 36, big -> registers 1..3, value -2
        # (same hand-computed layout as above, shifted by one register):
        # reg1 = 0x00FF, reg2 = 0xFFFF, reg3 = 0xFFE0.
        device = DeviceMirror(
            5,
            initial={0: 0xFFFF, 1: 0x00FF, 2: 0xFFFF, 3: 0xFFE0, 4: 2},
        )
        group = BitFieldGroup("g", version_address=4, table=self._table())
        snap = Scanner(ScanConfig(max_attempts=2)).scan(device, [group])
        values = {v.name: v for v in snap["g"].values}
        self.assertEqual(values["raw16"].raw, 0xFFFF)
        self.assertEqual(values["signed16"].raw, -1)
        self.assertEqual(values["wide"].raw, -2)
        self.assertEqual(values["wide"].registers, (1, 2, 3))
        self.assertEqual(values["wide"].masks, (0x00FF, 0xFFFF, 0xFFF0))

    def test_generation_conflict_yields_no_values(self):
        # Version stays odd forever: the gate never commits and the
        # error carries no decoded field values.
        script = [Change.set_version(4, 1)] * 8
        device = DeviceMirror(5, script=script, initial={4: 1})
        group = BitFieldGroup("g", version_address=4, table=self._table())
        with self.assertRaises(InconsistentScanError) as ctx:
            Scanner(ScanConfig(max_attempts=3)).scan(device, [group])
        self.assertFalse(hasattr(ctx.exception, "values"))

    def test_version_register_overlap_rejected(self):
        with self.assertRaises(ConfigError):
            BitFieldGroup("g", version_address=1, table=self._table())


if __name__ == "__main__":
    unittest.main()
