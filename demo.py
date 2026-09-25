"""Offline demo of the regmirror atomic scanner with read planning.

Everything is computed live against the deterministic scripted
``DeviceMirror`` (no sleeps, no prerecorded output; well under 90 s):

1. Inspectable plan: fields that overlap and sit close together are
   covered by fewer reads; the plan (blocks, read count, extra words) is
   printed before the scan, and a read log proves the device was touched
   exactly as planned.
2. Forbidden hole: a banned interval splits one block into two; a
   guarded image raises if any read enters the hole; the scanner also
   survives a device commit mid-attempt by re-running the *same* plan
   without reusing the failed attempt's partial blocks.
3. Rejection boundary: a version register stuck odd makes every attempt
   fail the gate, so ``InconsistentScanError`` is actually raised.
"""

from regmirror import (
    Change,
    DeviceMirror,
    FieldSpec,
    InconsistentScanError,
    RegisterGroup,
    ScanConfig,
    Scanner,
)


class ReadLogger:
    """Wraps a DeviceMirror, recording every raw read for display."""

    def __init__(self, dev):
        self._dev = dev
        self.size = dev.size
        self.log = []

    def read(self, start, count):
        words = self._dev.read(start, count)
        self.log.append((start, count))
        return words


def show_plan(plan):
    blocks = ", ".join(
        f"read({b.start}, {b.count})" for b in plan.blocks
    ) or "(none)"
    print(f"  plan for {plan.name!r}: {blocks}")
    print(
        f"  data reads/attempt: {plan.data_read_count} "
        f"(+2 version reads = {plan.attempt_reads}), "
        f"extra gap registers: {plan.extra_words}, "
        f"covered registers: {plan.covered_words}"
    )


def main():
    print("=== 1. Planned merge: overlapping/adjacent fields, fewer reads ===")
    # Register map (image size 16):
    #   0..3  energy  u64
    #   2..5  mirror  u64 (deliberately overlaps energy at 2..3)
    #   6     version register
    energy = FieldSpec("energy", 0, width=64)
    mirror = FieldSpec("mirror", 2, width=64)
    group = RegisterGroup(
        "boiler", 6, (energy, mirror), allow_overlap=True
    )
    initial = {
        0: 0x0102, 1: 0x0304, 2: 0x0506, 3: 0x0708,
        4: 0x090A, 5: 0x0B0C, 6: 2,
    }
    dev = DeviceMirror(16, initial=initial)
    scanner = Scanner(ScanConfig(max_read_words=6, max_gap_words=0))
    plan = scanner.build_plan(dev, [group])["boiler"]
    show_plan(plan)
    assert plan.data_read_count == 1 and plan.extra_words == 0
    logger = ReadLogger(dev)
    snap = scanner.scan(logger, [group])
    print(f"  device calls: {logger.log}  (version, one block, version)")
    g = snap["boiler"]
    print(f"  committed on attempt {g.attempts}, version {g.version}")
    print(f"  energy.raw = {g['energy'].raw:#018x}")
    print(f"  mirror.raw = {g['mirror'].raw:#018x}")
    assert g["energy"].raw == 0x0102030405060708
    assert g["mirror"].raw == 0x05060708090A0B0C

    print()
    print("=== 2. Forbidden hole forces a split; mid-attempt commit retried ===")
    # Fields 0..1 and 5..6; registers 2..4 are a forbidden hole, and
    # register 7 is the version.  max_read_words=9 would allow one read
    # 0..6, but that read crosses the hole, so the planner splits.
    a = FieldSpec("a", 0)
    b = FieldSpec("b", 5)
    split_group = RegisterGroup(
        "split", 7, (a, b), forbidden_ranges=((2, 5),)
    )

    class GuardedMirror(DeviceMirror):
        def read(self, start, count):
            for addr in range(start, start + count):
                if 2 <= addr < 5:
                    raise AssertionError(
                        f"forbidden register {addr} must not be read"
                    )
            return super().read(start, count)

    # The device commits once (version 2 -> 4, field b changes) right
    # after the first version read of attempt 1: that attempt is torn
    # and discarded, then the identical plan re-runs and commits.
    dev = GuardedMirror(
        16,
        script=[
            (
                Change.set_version(7, 3),
                Change.set_words(5, (0x00, 0x03)),
                Change.set_version(7, 4),
            ),
        ],
        initial={0: 0x00, 1: 0x01, 5: 0x00, 6: 0x02, 7: 2},
    )
    scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=9,
                                 max_gap_words=4))
    plan = scanner.build_plan(dev, [split_group])["split"]
    show_plan(plan)
    assert [blk.as_tuple() for blk in plan.blocks] == [(0, 2), (5, 2)]
    logger = ReadLogger(dev)
    snap = scanner.scan(logger, [split_group])
    print(
        "  device calls across both attempts:\n  "
        f"{logger.log}"
    )
    # Attempt 1: v,[0..1],[5..6],v (rejected 2->4); attempt 2 repeats
    # the same four reads from scratch -- no block from attempt 1 is
    # reused.  No call ever covers 2..4.
    assert logger.log == [
        (7, 1), (0, 2), (5, 2), (7, 1),
        (7, 1), (0, 2), (5, 2), (7, 1),
    ]
    g = snap["split"]
    print(
        f"  committed on attempt {g.attempts}, version {g.version}: "
        f"a.raw={g['a'].raw:#06x}, b.raw={g['b'].raw:#06x}"
    )
    assert g.attempts == 2 and g.version == 4 and g["b"].raw == 0x0003

    print()
    print("=== 3. Version stuck odd: rejection boundary actually raised ===")
    dev = DeviceMirror(16, initial={0: 0, 1: 0, 5: 0, 6: 0, 7: 3})
    try:
        Scanner(ScanConfig(max_attempts=4, max_read_words=9)).scan(
            dev, [split_group]
        )
    except InconsistentScanError as exc:
        print(f"  InconsistentScanError: {exc}")
        print(f"  attempts used: {exc.attempts}")
        print(f"  reason carries version words only -> {exc.reasons[0]}")
    else:
        raise AssertionError("expected InconsistentScanError")

    print()
    print("demo finished: 1 merged plan, 1 forbidden-hole split with a")
    print("full-plan retry, 1 rejection boundary -- all computed live.")


if __name__ == "__main__":
    main()
