"""Tests for the version-gated batched bit writes (regmirror.writer).

Expected register states come from RefModel, an independent reference
that applies bit operations with plain integer arithmetic -- it never
calls the merge/prepare logic under test.
"""

from __future__ import annotations

import unittest

from regmirror import (
    AddressError,
    BitWrite,
    Change,
    ConfigError,
    DeviceMirror,
    InconsistentScanError,
    VersionConflictError,
    commit_write_plan,
    prepare_write_plan,
)

VERSION_ADDR = 6


class RefModel:
    """Independent reference: plain-int bit ops on a register dict."""

    def __init__(self, initial: dict[int, int]):
        self.regs = dict(initial)

    def write_bits(self, address: int, mask: int, value: int) -> None:
        old = self.regs.get(address, 0)
        self.regs[address] = (old & (0xFFFF ^ mask)) | (value & mask)


def make_device(initial: dict[int, int], script=()) -> DeviceMirror:
    return DeviceMirror(size=32, script=script, initial=initial)


class TestBitWriteValidation(unittest.TestCase):
    def test_rejects_bool_and_non_int(self):
        for bad in (True, 1.5, "1"):
            with self.assertRaises(ConfigError):
                BitWrite(bad, 0x1, 0x1)
            with self.assertRaises(ConfigError):
                BitWrite(0, bad, 0x1)
            with self.assertRaises(ConfigError):
                BitWrite(0, 0x1, bad)

    def test_rejects_zero_mask(self):
        with self.assertRaises(ConfigError):
            BitWrite(0, 0x0, 0x0)

    def test_rejects_value_bits_outside_mask(self):
        with self.assertRaises(ConfigError):
            BitWrite(0, 0x00F0, 0x0100)

    def test_rejects_out_of_range_words(self):
        with self.assertRaises(ConfigError):
            BitWrite(0, 0x1_0000, 0x0)
        with self.assertRaises(ConfigError):
            BitWrite(-1, 0x1, 0x1)


class TestPreparePlan(unittest.TestCase):
    def test_merges_compatible_overlaps(self):
        dev = make_device({VERSION_ADDR: 10, 3: 0xA0A0})
        plan = prepare_write_plan(
            dev,
            VERSION_ADDR,
            [
                BitWrite(3, 0x00FF, 0x0003),
                BitWrite(3, 0x000F, 0x0003),  # compatible sub-mask
                BitWrite(3, 0x0F00, 0x0500),  # disjoint bits, same register
            ],
        )
        self.assertEqual(len(plan), 1)
        w = plan[3]
        self.assertEqual(w.mask, 0x0FFF)
        self.assertEqual(w.old, 0xA0A0)
        # independent reference for the merged write
        ref = RefModel({3: 0xA0A0})
        ref.write_bits(3, 0x00FF, 0x0003)
        ref.write_bits(3, 0x0F00, 0x0500)
        self.assertEqual(w.new, ref.regs[3])
        self.assertEqual(plan.version, 10)

    def test_conflict_rejected_before_any_read(self):
        dev = make_device({VERSION_ADDR: 10})
        with self.assertRaises(ConfigError):
            prepare_write_plan(
                dev,
                VERSION_ADDR,
                [BitWrite(3, 0x0001, 0x0001), BitWrite(3, 0x0001, 0x0000)],
            )
        self.assertEqual(dev.read_count, 0)  # failed before touching device

    def test_unselected_bits_preserved(self):
        dev = make_device({VERSION_ADDR: 4, 5: 0xABCD})
        plan = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(5, 0x0F0F, 0x0505)])
        ref = RefModel({5: 0xABCD})
        ref.write_bits(5, 0x0F0F, 0x0505)
        self.assertEqual(plan[5].new, ref.regs[5])
        self.assertEqual(plan[5].new & 0xF0F0, 0xABCD & 0xF0F0)

    def test_empty_and_wrong_items_rejected(self):
        dev = make_device({VERSION_ADDR: 4})
        with self.assertRaises(ConfigError):
            prepare_write_plan(dev, VERSION_ADDR, [])
        with self.assertRaises(ConfigError):
            prepare_write_plan(dev, VERSION_ADDR, [(3, 0x1, 0x1)])
        with self.assertRaises(ConfigError):
            prepare_write_plan(dev, True, [BitWrite(3, 0x1, 0x1)])

    def test_address_outside_image_rejected(self):
        dev = make_device({VERSION_ADDR: 4})
        with self.assertRaises(AddressError):
            prepare_write_plan(dev, VERSION_ADDR, [BitWrite(32, 0x1, 0x1)])
        with self.assertRaises(AddressError):
            prepare_write_plan(dev, 40, [BitWrite(3, 0x1, 0x1)])

    def test_version_gate_failure_yields_no_plan(self):
        # version flips odd right after the first version read
        script = [Change.set_version(VERSION_ADDR, 7)]
        dev = make_device({VERSION_ADDR: 6, 3: 0x1111}, script=script)
        with self.assertRaises(InconsistentScanError) as ctx:
            prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0x1, 0x1)])
        self.assertFalse(hasattr(ctx.exception, "values"))


class TestCommit(unittest.TestCase):
    def test_commit_applies_batch_and_bumps_version(self):
        dev = make_device({VERSION_ADDR: 10, 3: 0x00FF, 4: 0xFF00})
        plan = prepare_write_plan(
            dev,
            VERSION_ADDR,
            [BitWrite(3, 0x0F0F, 0x0505), BitWrite(4, 0x00FF, 0x0012)],
        )
        new_version = commit_write_plan(dev, plan)
        self.assertEqual(new_version, 12)
        ref = RefModel({3: 0x00FF, 4: 0xFF00})
        ref.write_bits(3, 0x0F0F, 0x0505)
        ref.write_bits(4, 0x00FF, 0x0012)
        self.assertEqual(dev.peek(3, 2), (ref.regs[3], ref.regs[4]))
        self.assertEqual(dev.peek(VERSION_ADDR, 1), (12,))

    def test_version_wraps_mod_2_16(self):
        dev = make_device({VERSION_ADDR: 0xFFFE, 3: 0})
        plan = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0x1, 0x1)])
        self.assertEqual(commit_write_plan(dev, plan), 0)
        self.assertEqual(dev.peek(VERSION_ADDR, 1), (0,))

    def test_consumed_plan_cannot_be_committed_twice(self):
        dev = make_device({VERSION_ADDR: 10, 3: 0})
        plan = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0x1, 0x1)])
        commit_write_plan(dev, plan)
        with self.assertRaises(ConfigError):
            commit_write_plan(dev, plan)

    def test_rejected_plan_writes_nothing_and_is_consumed(self):
        dev = make_device({VERSION_ADDR: 10, 3: 0x1234})
        plan = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0xFFFF, 0xAAAA)])
        # someone else commits in between (via a second plan)
        other = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(4, 0x1, 0x1)])
        commit_write_plan(dev, other)
        before = dev.peek(0, 8)
        with self.assertRaises(VersionConflictError) as ctx:
            commit_write_plan(dev, plan)
        self.assertEqual(ctx.exception.expected, 10)
        self.assertEqual(ctx.exception.actual, 12)
        self.assertEqual(dev.peek(0, 8), before)  # nothing written
        with self.assertRaises(ConfigError):  # no blind retry of stale plan
            commit_write_plan(dev, plan)


class TestTwoClientInterleaving(unittest.TestCase):
    """Two clients prepare plans against the same version, interleaved."""

    def test_disjoint_bits_both_survive(self):
        initial = {VERSION_ADDR: 20, 8: 0x0000}
        dev = make_device(initial)
        ref = RefModel({8: 0x0000})

        plan_a = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(8, 0x00FF, 0x0033)])
        plan_b = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(8, 0x0F00, 0x0500)])
        commit_write_plan(dev, plan_a)  # A wins the race
        ref.write_bits(8, 0x00FF, 0x0033)
        with self.assertRaises(VersionConflictError):
            commit_write_plan(dev, plan_b)  # B is stale: rejected, no retry
        # B re-reads the mirror and prepares a fresh plan
        plan_b2 = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(8, 0x0F00, 0x0500)])
        self.assertEqual(plan_b2[8].old, ref.regs[8])  # sees A bits
        commit_write_plan(dev, plan_b2)
        ref.write_bits(8, 0x0F00, 0x0500)
        self.assertEqual(dev.peek(8, 1), (ref.regs[8],))  # no lost update

    def test_conflicting_bits_last_committer_wins_after_reread(self):
        dev = make_device({VERSION_ADDR: 20, 9: 0x0000})
        ref = RefModel({9: 0x0000})

        plan_a = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(9, 0x0003, 0x0001)])
        plan_b = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(9, 0x0003, 0x0002)])
        commit_write_plan(dev, plan_b)  # B commits first this time
        ref.write_bits(9, 0x0003, 0x0002)
        with self.assertRaises(VersionConflictError):
            commit_write_plan(dev, plan_a)
        plan_a2 = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(9, 0x0003, 0x0001)])
        commit_write_plan(dev, plan_a2)
        ref.write_bits(9, 0x0003, 0x0001)
        self.assertEqual(dev.peek(9, 1), (ref.regs[9],))

    def test_cross_word_field_no_lost_update(self):
        # 32-bit field over registers 10 (high word) and 11 (low word);
        # client A patches bits in the low word, client B in the high.
        dev = make_device({VERSION_ADDR: 20, 10: 0xBEEF, 11: 0xCAFE})
        ref = RefModel({10: 0xBEEF, 11: 0xCAFE})

        plan_a = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(11, 0xFF00, 0x1200)])
        plan_b = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(10, 0x00FF, 0x0034)])
        commit_write_plan(dev, plan_a)
        ref.write_bits(11, 0xFF00, 0x1200)
        with self.assertRaises(VersionConflictError):
            commit_write_plan(dev, plan_b)
        plan_b2 = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(10, 0x00FF, 0x0034)])
        commit_write_plan(dev, plan_b2)
        ref.write_bits(10, 0x00FF, 0x0034)

        hi, lo = dev.peek(10, 2)
        self.assertEqual((hi, lo), (ref.regs[10], ref.regs[11]))
        # independent 32-bit reassembly check
        self.assertEqual((hi << 16) | lo, (ref.regs[10] << 16) | ref.regs[11])
        self.assertEqual(lo & 0x00FF, 0xCAFE & 0x00FF)  # A kept B-free bits

    def test_cross_word_single_plan_is_atomic(self):
        # one plan spanning both words of a 32-bit field commits together
        dev = make_device({VERSION_ADDR: 20, 10: 0x0000, 11: 0x0000})
        plan = prepare_write_plan(
            dev,
            VERSION_ADDR,
            [BitWrite(10, 0xFFFF, 0x1234), BitWrite(11, 0xFFFF, 0x5678)],
        )
        commit_write_plan(dev, plan)
        hi, lo = dev.peek(10, 2)
        self.assertEqual(((hi << 16) | lo), 0x12345678)


if __name__ == "__main__":
    unittest.main()
