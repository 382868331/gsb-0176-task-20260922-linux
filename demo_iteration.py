"""Demo: version-gated batched bit writes against a local device image.

Runs in well under 90 seconds, computes everything live (no sleeps, no
prerecorded answers):

1. prepare a write plan (merged mask / old / new / version) and commit
2. two clients racing on the same registers: disjoint bits, conflicting
   bits, and a 32-bit field split across two registers
3. the loser is rejected by compare-version-and-write, re-reads, and
   commits a fresh plan -- no lost updates, no blind retries

Run:  python demo_iteration.py
"""

from regmirror import (
    BitWrite,
    DeviceMirror,
    VersionConflictError,
    commit_write_plan,
    prepare_write_plan,
)

VERSION_ADDR = 6


def show_plan(title, plan):
    print(f"  {title} (version={plan.version})")
    for w in plan.writes:
        print(
            f"    reg {w.address:>2}: mask=0x{w.mask:04X} "
            f"old=0x{w.old:04X} new=0x{w.new:04X}"
        )


def main() -> None:
    print("regmirror write-plan demo (local state machine, live computation)")

    # -- 1. simple batched bit write with merged overlapping items ------
    print("\n[1] prepare + commit one batch (overlapping items merged)")
    dev = DeviceMirror(
        size=32,
        initial={VERSION_ADDR: 40, 3: 0xA0A0, 10: 0xBEEF, 11: 0xCAFE},
    )
    plan = prepare_write_plan(
        dev,
        VERSION_ADDR,
        [
            BitWrite(3, 0x00FF, 0x0033),
            BitWrite(3, 0x000F, 0x0003),  # compatible overlap, merges
        ],
    )
    show_plan("plan", plan)
    version = commit_write_plan(dev, plan)
    print(f"  committed, version {plan.version} -> {version}; "
          f"reg 3 = 0x{dev.peek(3, 1)[0]:04X} (unselected bits kept)")

    # -- 2. two clients racing ------------------------------------------
    print("\n[2] two clients interleaved on the same registers")
    # Client A wants low-byte bits of reg 3, client B high-nibble bits;
    # both prepare from the same version, then race.
    plan_a = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0x00F0, 0x0050)])
    plan_b = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(3, 0xF000, 0x5000)])
    show_plan("client A plan", plan_a)
    show_plan("client B plan", plan_b)
    commit_write_plan(dev, plan_a)
    print(f"  client A committed first (version now {dev.peek(VERSION_ADDR, 1)[0]})")
    try:
        commit_write_plan(dev, plan_b)
    except VersionConflictError as exc:
        print(f"  client B rejected: {exc}")
        print("  B does NOT retry the stale plan; it re-reads and replans")
        plan_b = prepare_write_plan(
            dev, VERSION_ADDR, [BitWrite(3, 0xF000, 0x5000)]
        )
        show_plan("client B fresh plan", plan_b)
        commit_write_plan(dev, plan_b)
    print(f"  reg 3 = 0x{dev.peek(3, 1)[0]:04X} -> both clients' bits present")

    # -- 3. conflicting bits: last committer wins, nothing lost silently -
    print("\n[3] conflicting bit values in one batch are rejected up front")
    try:
        prepare_write_plan(
            dev,
            VERSION_ADDR,
            [BitWrite(4, 0x0001, 0x0001), BitWrite(4, 0x0001, 0x0000)],
        )
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}")

    # -- 4. 32-bit field across two registers ----------------------------
    print("\n[4] 32-bit field (regs 10-11) patched by two racing clients")
    plan_a = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(11, 0xFF00, 0x1200)])
    plan_b = prepare_write_plan(dev, VERSION_ADDR, [BitWrite(10, 0x00FF, 0x0034)])
    commit_write_plan(dev, plan_a)
    try:
        commit_write_plan(dev, plan_b)
    except VersionConflictError:
        plan_b = prepare_write_plan(
            dev, VERSION_ADDR, [BitWrite(10, 0x00FF, 0x0034)]
        )
        commit_write_plan(dev, plan_b)
    hi, lo = dev.peek(10, 2)
    print(f"  field = 0x{hi:04X}{lo:04X} "
          f"(A wrote low-word bits, B high-word bits, none lost)")
    print(f"  final version = {dev.peek(VERSION_ADDR, 1)[0]}")


if __name__ == "__main__":
    main()
