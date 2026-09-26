"""Demo: bit-level field tables decoded from one verified register mirror.

Runs fully offline against the deterministic DeviceMirror and computes
everything live (no sleeps, no prerecorded output):

1. a normal scan_layout commit, including a signed field spanning parts
   of two registers and a 4-bit mode nibble;
2. boundary layouts: a negative value across a three-register span in
   big word order, and a signed field whose top bit is exactly the MSB
   of a register;
3. two legal union views of the *same* committed mirror;
4. the rejection path: an overlapping non-union layout (ConfigError
   before any device read) and a permanent generation conflict
   (InconsistentScanError with no partial values);
5. the original raw-register interface (FieldSpec/RegisterGroup) still
   working on the same device image.
"""

from regmirror import (
    BitFieldSpec,
    BitLayout,
    Change,
    ConfigError,
    DeviceMirror,
    FieldSpec,
    InconsistentScanError,
    RegisterGroup,
    ScanConfig,
    Scanner,
)


def show(snapshot):
    print(
        f"  layout {snapshot.name!r}: committed at version "
        f"{snapshot.version} after {snapshot.attempts} attempt(s)"
    )
    for v in snapshot.values:
        regs = ", ".join(f"reg{a} mask 0x{m:04X}" for a, m in v.registers)
        print(f"    {v.name:<10} = {v.value:<10} [{regs}]")


def main():
    scanner = Scanner(ScanConfig(max_attempts=4, max_read_words=2))

    print("1. normal commit: signed field across two registers + nibble")
    # little word order, base 4: logical = reg5<<16 | reg4.
    # temp = bits [4,24) signed; mode = bits [24,28).
    layout1 = BitLayout(
        "boiler-bits",
        version_address=2,
        base_address=4,
        fields=[
            BitFieldSpec("temp", start_bit=4, width=20, signed=True),
            BitFieldSpec("mode", start_bit=24, width=4),
        ],
        word_order="little",
    )
    dev = DeviceMirror(8, initial={2: 10, 4: 0x1234, 5: 0xABCD})
    show(scanner.scan_layout(dev, layout1))

    print("2. boundaries: 3-register span (big order) and MSB-aligned sign")
    # big order, base 0: logical = reg0<<32 | reg1<<16 | reg2.
    # wide = bits [4,44) -> spans all three registers.
    # hi   = bits [16,32) of a second layout -> top bit exactly the MSB.
    layout2 = BitLayout(
        "wide",
        version_address=6,
        base_address=0,
        fields=[BitFieldSpec("wide", start_bit=4, width=40)],
        word_order="big",
    )
    dev2 = DeviceMirror(
        8, initial={6: 2, 0: 0xABCD, 1: 0x1234, 2: 0xF000}
    )
    show(scanner.scan_layout(dev2, layout2))
    layout3 = BitLayout(
        "aligned",
        version_address=6,
        base_address=0,
        fields=[BitFieldSpec("hi", start_bit=16, width=16, signed=True)],
        word_order="big",
    )
    dev3 = DeviceMirror(8, initial={6: 4, 0: 0x8000, 1: 0x0001})
    show(scanner.scan_layout(dev3, layout3))

    print("3. two legal union views of the same mirror")
    union_layout = BitLayout(
        "views",
        version_address=6,
        base_address=0,
        fields=[
            BitFieldSpec("raw_u16", 0, 16, union="reg0"),
            BitFieldSpec("hi_s8", 8, 8, signed=True, union="reg0"),
            BitFieldSpec("alarm", 15, 1, union="reg0"),
        ],
    )
    dev4 = DeviceMirror(8, initial={6: 8, 0: 0x80FE})
    show(scanner.scan_layout(dev4, union_layout))

    print("4. rejections: illegal layout and permanent conflict")
    try:
        BitLayout(
            "bad",
            version_address=6,
            base_address=0,
            fields=[BitFieldSpec("a", 0, 16), BitFieldSpec("b", 8, 8)],
        )
    except ConfigError as exc:
        print(f"  overlapping non-union layout rejected: {exc}")
    conflict_layout = BitLayout(
        "busy",
        version_address=6,
        base_address=0,
        fields=[BitFieldSpec("a", 0, 16)],
    )
    busy = DeviceMirror(8, initial={6: 7, 0: 0x00FE})  # version stays odd
    try:
        Scanner(ScanConfig(max_attempts=3)).scan_layout(busy, conflict_layout)
    except InconsistentScanError as exc:
        print(f"  generation conflict, no partial values: {exc}")

    print("5. original raw-register interface on the same image kind")
    temp32 = FieldSpec(
        "temp32", address=4, width=32, signed=True, word_order="little"
    )
    group = RegisterGroup("boiler", version_address=2, fields=(temp32,))
    dev5 = DeviceMirror(
        8,
        script=[Change.write_field(temp32, -208605)],
        initial={2: 10},
    )
    snap = scanner.scan(dev5, [group])
    fv = snap["boiler"]["temp32"]
    print(f"  temp32 raw={fv.raw} value={fv.value} (old interface intact)")


if __name__ == "__main__":
    main()
