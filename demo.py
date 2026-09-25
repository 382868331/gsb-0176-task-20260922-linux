"""Offline demo of the regmirror atomic scanner.

Runs real scans against deterministic scripted device images:

1. A clean scan that commits on the first attempt (normal result).
2. A scan where the device rewrites a 64-bit field between two chunked
   reads -- the torn intermediate read is shown and discarded, the
   retry commits the consistent state.
3. A device whose version register stays odd forever -- the scan
   actually raises InconsistentScanError (rejection boundary).
4. The inspectable read planner: overlapping/gapped fields merged into
   minimal blocks, a forbidden hole splitting the plan, and the
   "impossible by construction" boundary (a required register inside a
   forbidden range -> ConfigError, before any device read).

Everything is computed live from the scripted image; no sleeps, no
prerecorded output.  Expected runtime: well under 90 seconds.
"""

from fractions import Fraction

from regmirror import (
    Change,
    ConfigError,
    DeviceMirror,
    FieldSpec,
    InconsistentScanError,
    RegisterGroup,
    ScanConfig,
    Scanner,
    decode_field,
)

# Register map (image size 16):
#   0..3  energy  u64, word big / byte big
#   4..5  temp    s32, word little / byte big, scale 1/10 (decidegrees)
#   6     version register (odd = device writing, even = stable)
ENERGY = FieldSpec("energy", 0, width=64)
TEMP = FieldSpec(
    "temp", 4, width=32, signed=True, word_order="little",
    scale=Fraction(1, 10),
)
GROUP = RegisterGroup("boiler", version_address=6, fields=(ENERGY, TEMP))

# Hand-computed register images (see README for the endianness model).
ENERGY_V1 = (0x0102, 0x0304, 0x0506, 0x0708)   # 0x0102030405060708
ENERGY_V2 = (0x0A0B, 0x0C0D, 0x0E0F, 0x1011)   # 0x0A0B0C0D0E0F1011
TEMP_V1 = (0x00EB, 0x0000)                     # raw 235  -> 23.5
TEMP_V2 = (0xFFD8, 0xFFFF)                     # raw -40  -> -4.0


def image(version):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = version
    return regs


def show_snapshot(snap):
    g = snap["boiler"]
    print(f"  committed on attempt {g.attempts}, version {g.version}")
    for v in g.values:
        # v.value is the exact Fraction; value_float is its correctly
        # rounded double (see README error statement).
        print(
            f"  {v.name:<7} raw={v.raw:<21} exact={str(v.value):<24} "
            f"float={v.value_float}"
        )


class ReadLogger:
    """Wraps a DeviceMirror, recording every raw read for display."""

    def __init__(self, dev):
        self._dev = dev
        self.size = dev.size
        self.log = []

    def read(self, start, count):
        words = self._dev.read(start, count)
        self.log.append((start, count, words))
        return words


def main():
    print("=== 1. Clean scan (normal result) ===")
    dev = DeviceMirror(16, initial=image(version=2))
    snap = Scanner(ScanConfig(max_read_words=2)).scan(dev, [GROUP])
    show_snapshot(snap)

    print()
    print("=== 2. Torn 64-bit read across chunks: discarded, retried ===")
    # max_read_words=2 splits the 6-word data span into 3 chunks.
    # After the first data chunk (read #1) the device atomically
    # rewrites the whole group and bumps the version 2 -> 4.
    dev = DeviceMirror(
        16,
        script=[
            (),  # read #0 (version): not yet
            (
                Change.set_version(6, 3),  # odd: write in progress
                Change.set_words(0, ENERGY_V2 + TEMP_V2),
                Change.set_version(6, 4),  # even: stable again
            ),  # lands right after read #1 -> tears the u64 field
        ],
        initial=image(version=2),
    )
    logger = ReadLogger(dev)
    snap = Scanner(ScanConfig(max_attempts=3, max_read_words=2)).scan(
        logger, [GROUP]
    )
    # Reconstruct what attempt 1 actually stitched together, from the
    # logged raw reads: version, chunk(0..1), chunk(2..3), chunk(4..5),
    # version.  The u64 spans chunks 1 and 2, i.e. old words 0..1 glued
    # to new words 2..3.
    r = logger.log
    torn_words = r[1][2] + r[2][2]
    torn = decode_field(ENERGY, torn_words)
    print(f"  attempt 1 raw reads: {[list(t[2]) for t in r[:5]]}")
    print(
        f"  attempt 1 saw version {r[0][2][0]} then {r[4][2][0]} "
        f"-> gate rejects"
    )
    print(f"  discarded torn u64: {torn.raw:#018x} (old high | new low)")
    print("  retry commits the consistent new state:")
    show_snapshot(snap)
    good = snap["boiler"]["energy"].raw
    assert good == 0x0A0B0C0D0E0F1011 and good != torn.raw

    print()
    print("=== 3. Version stuck odd: rejection boundary actually raised ===")
    dev = DeviceMirror(16, initial=image(version=3))  # odd forever
    try:
        Scanner(ScanConfig(max_attempts=4, max_read_words=2)).scan(dev, [GROUP])
    except InconsistentScanError as exc:
        print(f"  InconsistentScanError: {exc}")
        print(f"  attempts used: {exc.attempts}; no field values exposed:")
        print(f"  reasons carry version words only -> {exc.reasons[0]}")
    else:
        raise AssertionError("expected InconsistentScanError")

    print()
    print("=== 4. Read planner: overlaps, short gap, forbidden hole ===")
    # Two layouts, image size 16, version register at 8:
    #   group "ov": u64 both@0..3, u32 a@0..1 (overlap), u32 b@4..5
    #               -> occupied registers 0..5 (contiguous)
    #   group "gap": u32 a@0..1, u32 b@4..5 -> occupied 0..1,4..5
    #               with a 2-register gap (2,3) between them
    ov = RegisterGroup(
        "ov", version_address=8,
        fields=(
            FieldSpec("both", 0, width=64),
            FieldSpec("a", 0, width=32),
            FieldSpec("b", 4, width=32),
        ),
    )
    gap = RegisterGroup(
        "gap", version_address=8,
        fields=(FieldSpec("a", 0, width=32), FieldSpec("b", 4, width=32)),
    )
    initial = {0: 0x01, 1: 0x02, 2: 0x03, 3: 0x04,
               4: 0x05, 5: 0x06, 8: 2}

    def show_plan(title, group_, cfg):
        dev = DeviceMirror(16, initial=initial)
        plan = Scanner(cfg).plan_scan(dev, [group_])[group_.name]
        blocks = [(blk.start, blk.count) for blk in plan.blocks]
        print(f"  {title}")
        print(f"    blocks(start,count) = {blocks}")
        print(f"    data reads/attempt = {plan.read_count}, "
              f"extra registers = {plan.extra_registers}")
        plan.check()
        return dev

    # Overlapping fields: their occupied registers merge to 0..5, so one
    # block of 6 words reads everything, each shared register once.
    dev = show_plan(
        "overlapping fields (cap=8):", ov, ScanConfig(max_read_words=8)
    )
    snap = Scanner(ScanConfig(max_read_words=8)).scan(dev, [ov])
    print(f"    committed: both={snap['ov']['both'].raw:#018x}, "
          f"a={snap['ov']['a'].raw:#010x}, b={snap['ov']['b'].raw:#010x}")

    # Short gap: max_gap_words=2 allows bridging registers 2,3 in one
    # data read (2 extra registers); max_gap_words=0 forces two blocks.
    show_plan(
        "gap bridged (cap=8, max_gap_words=2):",
        gap, ScanConfig(max_read_words=8, max_gap_words=2),
    )
    show_plan(
        "gap not bridged (cap=8, max_gap_words=0):",
        gap, ScanConfig(max_read_words=8, max_gap_words=0),
    )

    # Forbidden hole 2..3: even when bridging is otherwise allowed, no
    # block may touch the hole, so the plan splits around it.
    show_plan(
        "forbidden hole 2..3 (cap=8, max_gap_words=2):",
        gap,
        ScanConfig(max_read_words=8, max_gap_words=2,
                   forbidden_ranges=[(2, 3)]),
    )

    # Boundary actually triggered: forbid a register a field occupies.
    # No legal plan exists -> ConfigError, before any device read.
    dev = DeviceMirror(16, initial=initial)
    bad_cfg = ScanConfig(forbidden_ranges=[(3, 3)])  # 3 is in "both"
    try:
        Scanner(bad_cfg).scan(dev, [ov])
    except ConfigError as exc:
        print(f"  impossible boundary -> ConfigError: {exc}")
        print(f"    device reads attempted: {dev.read_count} (must be 0)")
    else:
        raise AssertionError("expected ConfigError")
    assert dev.read_count == 0

    print()
    print("demo finished: 1 committed snapshot, 1 torn read discarded,")
    print("1 rejection boundary, planner overlap/gap/hole/impossible --")
    print("all computed live.")


if __name__ == "__main__":
    main()
