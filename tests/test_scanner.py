"""扫描器测试：跨读块撕裂、字序、奇数持续、版本回绕、重试耗尽。

变更插入点按设备 read() 调用序号编号。一组一次尝试的读序列固定为：
  调用 2k-1: read(version,1)；中间若干次: 数据块；最后: read(version,1)。
期望值全部以寄存器字面值/整数字面量给出。
"""

from __future__ import annotations

import unittest

from regscan import (
    ConfigError,
    DeviceProtocolError,
    FieldSpec,
    GroupSpec,
    InconsistentScanError,
    RegisterScanner,
    ScriptedRegisterDevice,
)

VA = 0          # 组 A 版本寄存器地址
A0 = 1          # 64 位字段地址（4 个字：1..4）
B_VADDR = 10    # 组 B（多字段）版本寄存器


def make_u64_group(block_words=None):
    g = GroupSpec(
        "g64",
        version_addr=VA,
        fields=(FieldSpec("q", A0, "u64", "high", "big"),),
    )
    return g


def regs_for(value64, vaddr=VA, version=0):
    """独立构造：64 位 big-word/big-byte 值 + 版本号的寄存器映像。"""
    image = {vaddr: version}
    image.update(data_for(value64))
    return image


def data_for(value64):
    """64 位 big-word/big-byte 值在 A0..A3 的数据映像（不含版本寄存器）。"""
    return {A0 + i: (value64 >> (48 - 16 * i)) & 0xFFFF for i in range(4)}


class HappyPathTests(unittest.TestCase):
    def test_static_snapshot(self):
        image = regs_for(0x0102030405060708)
        dev = ScriptedRegisterDevice(image)
        snap = RegisterScanner(dev, [make_u64_group()]).scan()
        self.assertEqual(snap.versions, {"g64": 0})
        fv = snap.fields["g64"]["q"]
        self.assertEqual(fv.raw, 0x0102030405060708)
        self.assertEqual(fv.value, float(0x0102030405060708))

    def test_multi_fields_word_order_and_fraction(self):
        fields = (
            FieldSpec("lo_word", 0, "u32", "low", "big", divisor=1000),
            FieldSpec("sval", 2, "s16"),
            FieldSpec("q", 3, "u64", "low", "little"),
        )
        g = GroupSpec("mix", version_addr=B_VADDR, fields=fields)
        image = {B_VADDR: 4}
        # u32 little-word: 1234000 -> words at 0/1
        v = 1_234_000
        image[0], image[1] = v & 0xFFFF, (v >> 16) & 0xFFFF
        image[2] = 0xFFF7  # -9 as s16
        qv = 0x1122334455667788
        # low-word + little-byte：地址从小到大
        for i in range(4):
            w = (qv >> (16 * i)) & 0xFFFF
            image[3 + i] = ((w & 0xFF) << 8) | (w >> 8)
        dev = ScriptedRegisterDevice(image)
        snap = RegisterScanner(dev, [g], read_block_size=3).scan()
        self.assertEqual(snap.versions["mix"], 4)
        self.assertEqual(snap.fields["mix"]["lo_word"].raw, 1_234_000)
        self.assertEqual(snap.fields["mix"]["lo_word"].value, 1234.0)
        self.assertEqual(snap.fields["mix"]["sval"].raw, -9)
        self.assertEqual(snap.fields["mix"]["q"].raw, qv)

    def test_segmented_reads_observe_block_boundaries(self):
        # 窗口 1..4（span 4），block=2 -> 数据读为 (1,2),(3,4)
        dev = ScriptedRegisterDevice(regs_for(0xDEADBEEFCAFEBABE))
        RegisterScanner(dev, [make_u64_group()], read_block_size=2).scan()
        self.assertEqual(
            dev.read_log,
            [(VA, 1), (A0, 2), (A0 + 2, 2), (VA, 1)],
        )


class TornReadTests(unittest.TestCase):
    """跨读块的 64 位撕裂必须被版本校验整体丢弃。"""

    def test_tear_between_blocks_rejected_then_retry_succeeds(self):
        old = 0xAAAAAAAAAAAAAAAA
        new = 0x5555555555555555
        dev = ScriptedRegisterDevice(regs_for(old, version=0))
        # 调用序：1=v1(0) 2=数据块(1,2 旧) 3=数据块(3,4 旧)<--此前提交
        #         4=v2(2 不一致) ; 第2次尝试: 5=v1 6=块1 7=块2 8=v2
        dev._events = {
            3: [("commit", VA, data_for(new))],  # 0->1->2 并换数据
        }
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=3, read_block_size=2)
        snap = scanner.scan()
        # 提交后版本为 2；值必须是完整的新值，绝不能是新旧拼接
        self.assertEqual(snap.versions["g64"], 2)
        self.assertEqual(snap.fields["g64"]["q"].raw, new)
        self.assertEqual(dev.calls, 8)

    def test_half_torn_even_with_matching_halves_is_still_unsafe_but_version_guards(self):
        # 数据在两个块之间变化且版本变化：必然 changed（即使两次数据巧合部分一致）
        dev = ScriptedRegisterDevice(regs_for(0x00000000FFFFFFFF, version=8))
        dev._events = {
            3: [("commit", VA, data_for(0xFFFFFFFF00000000))],  # 8->10
        }
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=1, read_block_size=2)
        with self.assertRaises(InconsistentScanError) as cm:
            scanner.scan()
        self.assertEqual(cm.exception.failures["g64"], ["changed"])

    def test_odd_version_at_second_read(self):
        # v1=0 偶、数据已读；第二次版本采样前设备 begin（v2=1 奇）-> writing
        dev = ScriptedRegisterDevice(regs_for(1))  # 默认版本 0
        dev._events = {3: [("begin", VA)]}
        scanner = RegisterScanner(dev, [make_u64_group()], max_attempts=1)
        with self.assertRaises(InconsistentScanError) as cm:
            scanner.scan()
        self.assertEqual(cm.exception.failures["g64"], ["writing"])
        self.assertEqual(dev.calls, 3)


class OddPersistTests(unittest.TestCase):
    def test_permanently_odd_exhausts_retries(self):
        # 版本恒为奇数（设备一直处于写入中），每次尝试只读一次版本
        dev = ScriptedRegisterDevice({VA: 7})
        scanner = RegisterScanner(dev, [make_u64_group()], max_attempts=4)
        with self.assertRaises(InconsistentScanError) as cm:
            scanner.scan()
        self.assertEqual(cm.exception.failures["g64"], ["writing"] * 4)
        self.assertEqual(cm.exception.attempts["g64"], 4)
        self.assertEqual(dev.calls, 4)

    def test_odd_then_settles_retried_within_budget(self):
        dev = ScriptedRegisterDevice(regs_for(0x1111111111111111, version=3))
        # 第 1 次尝试 v1=3 奇；第 2 次尝试开始前（调用 2）完成提交 3->4
        dev._events = {2: [("finish", VA)]}
        scanner = RegisterScanner(dev, [make_u64_group()], max_attempts=3)
        snap = scanner.scan()
        self.assertEqual(snap.versions["g64"], 4)
        self.assertEqual(snap.fields["g64"]["q"].raw, 0x1111111111111111)


class WrapTests(unittest.TestCase):
    def test_wrap_65534_to_zero_retry_succeeds(self):
        dev = ScriptedRegisterDevice(regs_for(0x2222222222222222, version=0xFFFE))
        # 第 1 次尝试数据读到一半时提交：FFFE->FFFF(奇)->0000(偶)，数据换新
        dev._events = {
            3: [("commit", VA, data_for(0x4444444444444444))],
        }
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=2, read_block_size=2)
        snap = scanner.scan()
        self.assertEqual(snap.versions["g64"], 0)
        self.assertEqual(snap.fields["g64"]["q"].raw, 0x4444444444444444)

    def test_wrap_through_odd_on_second_read(self):
        dev = ScriptedRegisterDevice(regs_for(0, version=0xFFFE))
        # v1=FFFE 偶；读完数据后设备仅 begin（v2=FFFF 奇）-> writing，重试
        dev._events = {
            4: [("begin", VA)],  # 第二次版本采样读到 FFFF（奇）
            5: [("setm", data_for(7)), ("finish", VA)],  # 重试前落定到 0
        }
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=2, read_block_size=2)
        snap = scanner.scan()
        self.assertEqual(snap.versions["g64"], 0)
        self.assertEqual(snap.fields["g64"]["q"].raw, 7)


    def test_wrap_alias_boundary_32768_commits_undetectable_by_protocol(self):
        """契约边界：两次版本采样之间恰好 32768 次提交时，偶数版本号重现。

        这是 16 位 seqlock 的固有限制（>32767 即超出契约），本测试显式记录
        该边界行为，而非掩盖它：纯寄存器观察无法区分“未变”与“变了整圈”。
        """
        dev = ScriptedRegisterDevice(regs_for(0xAAAAAAAAAAAAAAAA, version=0))

        def commit_32768_times(d) -> None:
            # 32768 次完整提交 = 65536 次版本 +1 -> 版本字回到 0；
            # 同时把数据换成新值，制造协议不可见的整圈别名。
            d.set_many(data_for(0x5555555555555555))
            d._regs[VA] = (d._regs[VA] + 65536) & 0xFFFF

        # 调用序：1=v1(0) 2=块1 3=块2(此前变更) 4=v2(0)
        dev._events = {3: [("call", commit_32768_times)]}
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=1, read_block_size=2)
        snap = scanner.scan()  # 契约外情形：不抛错，但结果是新旧拼接
        self.assertEqual(snap.versions["g64"], 0)
        self.assertEqual(snap.fields["g64"]["q"].raw, 0xAAAAAAAA55555555)


class ExhaustionAndAtomicityTests(unittest.TestCase):
    def test_always_changing_exhausts_with_changed_reasons(self):
        dev = ScriptedRegisterDevice(regs_for(0, version=0))
        # 每次尝试的第二次版本读之前都做一次提交 -> v2 永远 != v1
        dev._events = {4: [("commit", VA, data_for(1))],
                       8: [("commit", VA, data_for(2))],
                       12: [("commit", VA, data_for(3))]}
        scanner = RegisterScanner(dev, [make_u64_group()],
                                  max_attempts=3, read_block_size=2)
        with self.assertRaises(InconsistentScanError) as cm:
            scanner.scan()
        self.assertEqual(cm.exception.failures["g64"], ["changed"] * 3)

    def test_no_partial_values_on_failure_and_scanner_reusable(self):
        g_bad = make_u64_group()
        g_ok = GroupSpec(
            "ok",
            version_addr=20,
            fields=(FieldSpec("w", 21, "u16"),),
        )
        dev = ScriptedRegisterDevice({**regs_for(0), 20: 0, 21: 12345})
        dev._events = {4: [("commit", VA, data_for(1))],
                       8: [("commit", VA, data_for(2))]}
        scanner = RegisterScanner(dev, [g_bad, g_ok],
                                  max_attempts=2, read_block_size=2)
        with self.assertRaises(InconsistentScanError) as cm:
            scanner.scan()
        # 异常体只含诊断，不含任何字段值
        self.assertFalse(hasattr(cm.exception, "fields"))
        self.assertEqual(set(cm.exception.failures), {"g64"})

        # 扫描器无残留状态：设备静止后重扫成功
        dev2 = ScriptedRegisterDevice({**regs_for(9), 20: 6, 21: 42})
        scanner2 = RegisterScanner(dev2, [g_bad, g_ok], read_block_size=2)
        snap = scanner2.scan()
        self.assertEqual(snap.fields["g64"]["q"].raw, 9)
        self.assertEqual(snap.fields["ok"]["w"].raw, 42)
        self.assertEqual(snap.versions, {"g64": 0, "ok": 6})


class DeviceAndConfigTests(unittest.TestCase):
    def test_bad_word_from_device_raises_protocol_error(self):
        class BadDevice:
            def read(self, start, count):
                return [0x10000] * count

        scanner = RegisterScanner(BadDevice(), [make_u64_group()])
        with self.assertRaises(DeviceProtocolError):
            scanner.scan()

    def test_bad_length_from_device_raises_protocol_error(self):
        class ShortDevice:
            def read(self, start, count):
                return (0,) if start == VA else (0,) * (count - 1)

        scanner = RegisterScanner(ShortDevice(), [make_u64_group()])
        with self.assertRaises(DeviceProtocolError):
            scanner.scan()

    def test_scanner_config_validation(self):
        dev = ScriptedRegisterDevice({})
        with self.assertRaises(ConfigError):
            RegisterScanner(object(), [make_u64_group()])
        with self.assertRaises(ConfigError):
            RegisterScanner(dev, [])
        with self.assertRaises(ConfigError):
            RegisterScanner(dev, [make_u64_group()], max_attempts=0)
        with self.assertRaises(ConfigError):
            RegisterScanner(dev, [make_u64_group()], max_attempts=True)
        with self.assertRaises(ConfigError):
            RegisterScanner(dev, [make_u64_group()], read_block_size=126)
        with self.assertRaises(ConfigError):
            RegisterScanner(dev, [make_u64_group(), make_u64_group()])

    def test_group_spec_validation(self):
        f = FieldSpec("a", 0, "u32")
        with self.assertRaises(Exception):
            GroupSpec("g", version_addr=1, fields=(f,))  # 版本落在字段区间
        f2 = FieldSpec("b", 1, "u16")
        with self.assertRaises(Exception):
            GroupSpec("g", version_addr=9, fields=(f, f2))  # 重叠
        with self.assertRaises(Exception):
            GroupSpec("g", version_addr=9, fields=(f, f))   # 重名
        with self.assertRaises(Exception):
            GroupSpec("", version_addr=9, fields=(f2,))


if __name__ == "__main__":
    unittest.main()
