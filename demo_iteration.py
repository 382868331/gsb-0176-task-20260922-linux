"""Offline demo: bit-field tables decoded from one verified mirror.

Real computation, fixed inputs, no device/network/sleep.  Shows:

* one version-gated scan committing a single mirror, decoded through a
*   bit-field table with two legal union views of the same registers;
*   fields spanning three registers, negative values, both word orders
*   and an exactly MSB-aligned field -- each printed next to the
*   hand-computed bit string it must match;
*   boundary rejections: illegal overlapping layout, missing register
*   (no partial decode) and a generation conflict (no values at all).

Run:  python demo_iteration.py      (well under 90 seconds)
"""

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
)
from regmirror.errors import ConfigError, InconsistentScanError


def show(value):
    masks = ", ".join(
        f"reg {a} mask 0x{m:04X}" for a, m in zip(value.registers, value.masks)
    )
    print(f"    {value.name:<10} raw={value.raw:<12} {masks}")


def main():
    print("== 1. one verified mirror, decoded as bit-field table ==")
    # Hand-computed register image (see tests/test_bitfields.py):
    #   temp: offset 4, width 36, signed, big -> -2
    #         bits 1111...1110 -> reg0=0x00FF reg1=0xFFFF reg2=0xFFE0
    #   flow: offset 4, width 36, signed, little -> same bits, mirrored
    #         registers live at 4..6: reg4=0xFFE0 reg5=0xFFFF reg6=0x00FF
    #   align: offset 12, width 20, signed, big, MSB exactly on the
    #         register top -> -1 -> reg8=0xFFFF reg9=0xF000
    #   union views of reg 3: 0x8000 -> 32768 unsigned / -32768 signed
    table = BitFieldTable(
        0,
        [
            BitFieldSpec("temp", 4, 36, signed=True, word_order="big"),
            BitFieldSpec("raw_view", 48, 16, union="reg3"),
            BitFieldSpec("signed_view", 48, 16, signed=True, union="reg3"),
            BitFieldSpec("flow", 68, 36, signed=True, word_order="little"),
            BitFieldSpec("align", 140, 20, signed=True, word_order="big"),
        ],
    )
    initial = {
        0: 0x00FF, 1: 0xFFFF, 2: 0xFFE0,   # temp (big)
        3: 0x8000,                          # union views
        4: 0xFFE0, 5: 0xFFFF, 6: 0x00FF,   # flow (little)
        8: 0xFFFF, 9: 0xF000,              # align (MSB-aligned)
        10: 2,                              # version (even = stable)
    }
    device = DeviceMirror(11, initial=initial)
    group = BitFieldGroup("boiler", version_address=10, table=table)
    snap = Scanner(ScanConfig(max_attempts=3)).scan(device, [group])
    print(f"  committed at version {snap['boiler'].version}, "
          f"attempts={snap['boiler'].attempts}")
    for v in snap["boiler"].values:
        show(v)
    print("  hand-computed check: temp=-2 (0xFFFFFFFFE), flow=-2, "
          "align=-1, raw_view=32768, signed_view=-32768")

    print("== 2. boundary: illegal overlapping layout rejected pre-scan ==")
    try:
        BitFieldTable(
            0,
            [BitFieldSpec("a", 0, 16), BitFieldSpec("b", 8, 16)],
        )
    except ConfigError as exc:
        print(f"  rejected: {exc}")

    print("== 3. boundary: missing register -> no partial decode ==")
    try:
        decode_bitfields(table, {0: 0x00FF, 1: 0xFFFF})  # most words missing
    except IncompleteImageError as exc:
        print(f"  rejected: {exc}")

    print("== 4. boundary: generation conflict -> no values at all ==")
    # Device keeps the version odd (write in progress); the gate never
    # commits and the error carries no decoded values.
    busy = DeviceMirror(
        11, script=[Change.set_version(10, 1)] * 8, initial={10: 1}
    )
    try:
        Scanner(ScanConfig(max_attempts=3)).scan(busy, [group])
    except InconsistentScanError as exc:
        print(f"  rejected: {exc}")
        print(f"  exception carries decoded values: "
              f"{hasattr(exc, 'values')}")


if __name__ == "__main__":
    main()
