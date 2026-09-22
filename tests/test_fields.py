"""Codec tests: word/byte order, signedness, Fraction scaling.

Expected words in these tests are computed by hand from the documented
endianness model -- never by calling the package's own encoder -- so the
tests are an independent reference.  Round-trip checks are included only
as a supplement.
"""

import unittest
from fractions import Fraction

from regmirror import FieldSpec, decode_field, encode_field
from regmirror.errors import ConfigError


def spec(width=32, signed=False, word_order="big", byte_order="big", scale=1):
    return FieldSpec(
        "f",
        0,
        width=width,
        signed=signed,
        word_order=word_order,
        byte_order=byte_order,
        scale=scale,
    )


class TestWordByteOrder32(unittest.TestCase):
    # 0x01020304 laid out per the four order combinations, by hand.
    CASES = {
        ("big", "big"): (0x0102, 0x0304),
        ("big", "little"): (0x0201, 0x0403),
        ("little", "big"): (0x0304, 0x0102),
        ("little", "little"): (0x0403, 0x0201),
    }

    def test_decode_all_orders(self):
        for (wo, bo), words in self.CASES.items():
            with self.subTest(word_order=wo, byte_order=bo):
                v = decode_field(spec(word_order=wo, byte_order=bo), words)
                self.assertEqual(v.raw, 0x01020304)

    def test_encode_all_orders(self):
        for (wo, bo), words in self.CASES.items():
            with self.subTest(word_order=wo, byte_order=bo):
                got = encode_field(spec(word_order=wo, byte_order=bo), 0x01020304)
                self.assertEqual(got, words)


class TestWordByteOrder64(unittest.TestCase):
    CASES = {
        ("big", "big"): (0x0102, 0x0304, 0x0506, 0x0708),
        ("big", "little"): (0x0201, 0x0403, 0x0605, 0x0807),
        ("little", "big"): (0x0708, 0x0506, 0x0304, 0x0102),
        ("little", "little"): (0x0807, 0x0605, 0x0403, 0x0201),
    }

    def test_decode_all_orders(self):
        for (wo, bo), words in self.CASES.items():
            with self.subTest(word_order=wo, byte_order=bo):
                v = decode_field(
                    spec(width=64, word_order=wo, byte_order=bo), words
                )
                self.assertEqual(v.raw, 0x0102030405060708)

    def test_encode_all_orders(self):
        for (wo, bo), words in self.CASES.items():
            with self.subTest(word_order=wo, byte_order=bo):
                got = encode_field(
                    spec(width=64, word_order=wo, byte_order=bo),
                    0x0102030405060708,
                )
                self.assertEqual(got, words)


class TestSignedness(unittest.TestCase):
    def test_signed32_negative(self):
        # -2 as two's complement s32 = 0xFFFFFFFE, big/big by hand.
        v = decode_field(spec(signed=True), (0xFFFF, 0xFFFE))
        self.assertEqual(v.raw, -2)

    def test_unsigned32_same_words(self):
        v = decode_field(spec(signed=False), (0xFFFF, 0xFFFE))
        self.assertEqual(v.raw, 0xFFFFFFFE)

    def test_signed64_min(self):
        # -2**63 = 0x8000000000000000, big/big by hand.
        v = decode_field(
            spec(width=64, signed=True), (0x8000, 0x0000, 0x0000, 0x0000)
        )
        self.assertEqual(v.raw, -(1 << 63))

    def test_encode_signed_negative_roundtrip_supplement(self):
        # Supplementary round-trip only; the authoritative checks above
        # use hand-computed words.
        s = spec(width=64, signed=True, word_order="little", byte_order="little")
        for value in (-(1 << 63), -1, 0, 1, (1 << 63) - 1):
            self.assertEqual(decode_field(s, encode_field(s, value)).raw, value)

    def test_encode_out_of_range_rejected(self):
        with self.assertRaises(ConfigError):
            encode_field(spec(signed=False), 1 << 32)
        with self.assertRaises(ConfigError):
            encode_field(spec(signed=True), -(1 << 31) - 1)
        with self.assertRaises(ConfigError):
            encode_field(spec(signed=True), True)  # bool is not an int here


class TestScaling(unittest.TestCase):
    def test_fraction_scale_exact(self):
        s = spec(scale=Fraction(1, 1000))
        v = decode_field(s, (0x0001, 0xE240))  # 123456 by hand
        self.assertEqual(v.raw, 123456)
        self.assertEqual(v.value, Fraction(15432, 125))  # 123456/1000 reduced
        self.assertAlmostEqual(v.value_float, 123.456, places=12)

    def test_negative_scale_with_signed(self):
        s = spec(signed=True, scale=Fraction(-3, 2))
        v = decode_field(s, (0xFFFF, 0xFFFE))  # raw -2
        self.assertEqual(v.value, Fraction(3))

    def test_float_scale_converted_exactly(self):
        # 0.5 is exactly representable; conversion must be exact.
        s = spec(scale=0.5)
        self.assertEqual(s.scale, Fraction(1, 2))

    def test_nan_and_infinity_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    spec(scale=bad)

    def test_bool_scale_rejected(self):
        with self.assertRaises(ConfigError):
            spec(scale=True)


class TestFieldSpecValidation(unittest.TestCase):
    def test_bool_address_rejected(self):
        with self.assertRaises(ConfigError):
            FieldSpec("f", True)

    def test_bad_width_rejected(self):
        with self.assertRaises(ConfigError):
            FieldSpec("f", 0, width=16)

    def test_bad_order_rejected(self):
        with self.assertRaises(ConfigError):
            FieldSpec("f", 0, word_order="middle")
        with self.assertRaises(ConfigError):
            FieldSpec("f", 0, byte_order="MIDDLE")

    def test_address_bounds(self):
        with self.assertRaises(ConfigError):
            FieldSpec("f", -1)
        with self.assertRaises(ConfigError):
            FieldSpec("f", 65536)


if __name__ == "__main__":
    unittest.main()
