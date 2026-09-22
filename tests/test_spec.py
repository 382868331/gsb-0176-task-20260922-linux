"""FieldSpec 解码/编码与输入校验测试。

期望值约定：寄存器字布局全部以字面量给出，物理整数用十六进制字面量，
不调用被测 decode_words 来产生期望值；encode/decode 往返仅作补充验证。
"""

from __future__ import annotations

import unittest

from regscan import FieldSpec, FieldSpecError
from regscan.spec import _check_finite_number  # type: ignore  # 内部校验复用


def decode_independent(words, width, signed, word_order="high", byte_order="big"):
    """测试侧独立参考：纯移位拼字（与被测实现独立编写）。"""
    ordered = list(words) if word_order == "high" else list(reversed(words))
    value = 0
    for w in ordered:
        if byte_order == "little":
            w = ((w & 0xFF) << 8) | (w >> 8)
        value = (value << 16) | w
    if signed:
        value -= (value >> (width * 16 - 1)) << (width * 16)
    return value


class DecodeTests(unittest.TestCase):
    def test_u16_and_s16_literals(self):
        u = FieldSpec("u", 0, "u16")
        s = FieldSpec("s", 0, "s16")
        self.assertEqual(u.decode_words((0xFFFF,))[0], 65535)
        self.assertEqual(s.decode_words((0xFFFF,))[0], -1)
        self.assertEqual(s.decode_words((0x8000,))[0], -32768)
        self.assertEqual(s.decode_words((0x7FFF,))[0], 32767)

    def test_u32_word_and_byte_orders(self):
        spec_hi = FieldSpec("a", 0, "u32", "high", "big")
        spec_lo = FieldSpec("b", 0, "u32", "low", "big")
        spec_hi_le = FieldSpec("c", 0, "u32", "high", "little")
        spec_lo_le = FieldSpec("d", 0, "u32", "low", "little")
        self.assertEqual(spec_hi.decode_words((0x1234, 0x5678))[0], 0x12345678)
        self.assertEqual(spec_lo.decode_words((0x5678, 0x1234))[0], 0x12345678)
        self.assertEqual(spec_hi_le.decode_words((0x3412, 0x7856))[0], 0x12345678)
        self.assertEqual(spec_lo_le.decode_words((0x7856, 0x3412))[0], 0x12345678)

    def test_s32_negative(self):
        s = FieldSpec("s", 0, "s32", "high", "big")
        self.assertEqual(s.decode_words((0xFFFF, 0xFFFF))[0], -1)
        self.assertEqual(s.decode_words((0xFFFF, 0xFFFE))[0], -2)
        self.assertEqual(s.decode_words((0x8000, 0x0000))[0], -2147483648)

    def test_u64_s64_orders_literals(self):
        u_hi = FieldSpec("u", 0, "u64", "high", "big")
        u_lo = FieldSpec("u", 0, "u64", "low", "big")
        s_hi = FieldSpec("s", 0, "s64", "high", "big")
        self.assertEqual(
            u_hi.decode_words((0x0102, 0x0304, 0x0506, 0x0708))[0],
            0x0102030405060708,
        )
        self.assertEqual(
            u_lo.decode_words((0x0708, 0x0506, 0x0304, 0x0102))[0],
            0x0102030405060708,
        )
        self.assertEqual(
            s_hi.decode_words((0xFEFD, 0xFCFB, 0xFAF9, 0xF8F8))[0],
            -0x0102030405060708,
        )
        self.assertEqual(s_hi.decode_words((0x8000, 0, 0, 0))[0], -(1 << 63))

    def test_little_endian_bytes_in_each_word(self):
        spec = FieldSpec("a", 0, "u64", "high", "little")
        words = (0x0201, 0x0403, 0x0605, 0x0807)
        self.assertEqual(spec.decode_words(words)[0], 0x0102030405060708)

    def test_independent_oracle_matches(self):
        """与测试侧独立移位参考交叉核对（含随机样式的确定性取值）。"""
        cases = [
            ("u32", "high", "big", 0xDEADBEEF, False),
            ("u32", "low", "big", 0xCAFEBABE, False),
            ("s32", "high", "big", -123456789, True),
            ("u64", "high", "big", 0x0011223344556677, False),
            ("u64", "low", "big", 0xABCDEF0123456789, False),
            ("s64", "high", "big", -987654321098765, True),
        ]
        for dt, wo, bo, value, signed in cases:
            spec = FieldSpec("f", 0, dt, wo, bo)
            width = spec.width
            raw = value & ((1 << (width * 16)) - 1)
            words = []
            for shift in range(width * 16 - 16, -1, -16):
                w = (raw >> shift) & 0xFFFF
                if bo == "little":
                    w = ((w & 0xFF) << 8) | (w >> 8)
                words.append(w)
            if wo == "low":
                words.reverse()
            got_raw, got_scaled = spec.decode_words(tuple(words))
            self.assertEqual(got_raw, value)
            self.assertEqual(got_raw,
                             decode_independent(words, width, signed, wo, bo))

    def test_fraction_scaling(self):
        f = FieldSpec("t", 0, "s32", "high", "big", divisor=100)
        raw, scaled = f.decode_words((0x00, 0x04D2))  # 1234
        self.assertEqual(raw, 1234)
        # 正确舍入的 binary64：与字面量 12.34 逐位相同（非容差比较）
        self.assertEqual(scaled, 12.34)

        fneg = FieldSpec("p", 0, "s16", divisor=10)
        _, sv = fneg.decode_words((0xFFF6,))  # -10
        self.assertEqual(sv, -1.0)

    def test_fraction_non_integral_input_rejected(self):
        f = FieldSpec("t", 0, "u16", divisor=100)
        with self.assertRaises(FieldSpecError):
            f.encode_words(1.01)  # 1.01*100 不是整数
        with self.assertRaises(FieldSpecError):
            f.encode_words(float("nan"))
        with self.assertRaises(FieldSpecError):
            f.encode_words(float("inf"))
        with self.assertRaises(FieldSpecError):
            f.encode_words(-float("inf"))

    def test_round_trip_supplementary(self):
        # 补充验证：encode -> decode 往返（期望值仍以字面量为准）
        for dt, value in [("u32", 0x12345678), ("s32", -12345),
                          ("u64", 0x0102030405060708), ("s64", -777)]:
            f = FieldSpec("x", 0, dt, "low", "big", divisor=1)
            words = f.encode_words(value)  # int 精确输入，不经 float
            self.assertEqual(f.decode_words(words)[0], value)

    def test_encode_range_check(self):
        u16 = FieldSpec("u", 0, "u16")
        with self.assertRaises(FieldSpecError):
            u16.encode_words(65536)
        s16 = FieldSpec("s", 0, "s16")
        with self.assertRaises(FieldSpecError):
            s16.encode_words(-32769)


class ValidationTests(unittest.TestCase):
    def test_bool_rejected_everywhere(self):
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", True)  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, divisor=True)  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0).encode_words(True)  # type: ignore[arg-type]

    def test_bad_enum_and_name(self):
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, data_type="f32")  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, word_order="middle")  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, byte_order="small")  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("", 0)

    def test_bad_address_and_divisor(self):
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", -1)
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 1.5)  # type: ignore[arg-type]
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, divisor=0)
        with self.assertRaises(FieldSpecError):
            FieldSpec("a", 0, divisor=-2)

    def test_finite_number_checker_messages(self):
        with self.assertRaises(FieldSpecError):
            _check_finite_number(float("nan"), "x")
        with self.assertRaises(FieldSpecError):
            _check_finite_number("12", "x")  # type: ignore[arg-type]
        self.assertEqual(_check_finite_number(3, "x"), 3.0)


if __name__ == "__main__":
    unittest.main()
