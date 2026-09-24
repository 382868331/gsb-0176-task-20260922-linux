"""Offline demo of the regmirror atomic scanner.

Runs five real scans against deterministic scripted device images:

Single-device version-gated scanner:
1. A clean scan that commits on the first attempt (normal result).
2. A scan where the device rewrites a 64-bit field between two chunked
   reads -- the torn intermediate read is shown and discarded, the
   retry commits the consistent state.
3. A device whose version register stays odd forever -- the scan
   actually raises InconsistentScanError (rejection boundary).

Barrier mirror across several history-keeping devices:
4. Three devices commit at non-contiguous global seqs; a barrier mirror
   returns each device's last commit at or before the barrier -- one
   served from immutable history (zero device reads), two live.
5. A barrier older than retained history -> snapshot_unavailable, and a
   bus fault on one device -> the whole mirror is withheld.

Everything is computed live from the scripted images; no sleeps, no
prerecorded output.  Expected runtime: well under a second.
"""

from fractions import Fraction

from regmirror import (
    Change,
    DeviceCommit,
    DeviceReadError,
    DeviceMirror,
    FieldSpec,
    HistoricalDevice,
    InconsistentScanError,
    MirrorRequest,
    MirrorScanner,
    MultiDeviceReadError,
    RegisterGroup,
    ScanConfig,
    Scanner,
    SnapshotUnavailableError,
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


def image_data():
    # Data words only; HistoricalDevice takes the version separately.
    return dict(enumerate(ENERGY_V1 + TEMP_V1))


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
    print("=== 4. Barrier mirror across three history-keeping devices ===")
    # Global commit timeline (non-contiguous seqs are deliberate):
    #   A: seq1 V1/v2, seq5 V2/v4, seq9 V1/v6
    #   B: seq2 V1/v2 (no further commits)
    #   C: seq3 V1/v0, seq7 V2/v2
    # Barrier 6 picks A@5 and C@3 (both sealed history), B@2 (live).
    a = HistoricalDevice(
        "boiler-A", 16, 6, initial_version=2, initial=image_data(),
        start_seq=1,
        script=[
            DeviceCommit.at(4, [Change.set_words(0, ENERGY_V2 + TEMP_V2)], seq=5),
            DeviceCommit.at(6, [Change.set_words(0, ENERGY_V1 + TEMP_V1)], seq=9),
        ],
    )
    b = HistoricalDevice("boiler-B", 16, 6, initial_version=2,
                         initial=image_data(), start_seq=2)
    c = HistoricalDevice("boiler-C", 16, 6, initial_version=0,
                         initial=image_data(), start_seq=3,
                         script=[DeviceCommit.at(
                             2, [Change.set_words(0, ENERGY_V2 + TEMP_V2)], seq=7)])
    # Advance the scripts so A holds commits 1/5/9 and C holds 3/7.
    a.read(0, 1)
    a.read(0, 1)
    c.read(0, 1)
    reads_a_before = a.read_count
    devices = {"boiler-A": a, "boiler-B": b, "boiler-C": c}
    groups = {dev_id: GROUP for dev_id in devices}
    req = MirrorRequest("plant-snapshot", barrier=6,
                        devices=devices, groups=groups)
    mirror = MirrorScanner(ScanConfig(max_read_words=2)).snapshot_at(req)
    print(f"  mirror {mirror.name!r} barrier={mirror.barrier} "
          f"status={mirror.status}")
    for d in mirror.devices:
        fields = ", ".join(f"{v.name}={v.raw:#x}" for v in d.values)
        print(
            f"  {d.device_id:<9} seq={d.seq} version={d.version:<2} "
            f"source={d.source:<7} attempts={d.attempts}  {fields}"
        )
    print(f"  boiler-A added {a.read_count - reads_a_before} device reads "
          "(sealed commit decoded from immutable history)")
    assert mirror["boiler-A"].seq == 5 and mirror["boiler-A"].source == "history"
    assert mirror["boiler-B"]["energy"].raw == 0x0102030405060708
    assert mirror["boiler-C"]["energy"].raw == 0x0102030405060708

    print()
    print("=== 5a. Barrier older than retained history: snapshot_unavailable ===")
    # This device's oldest retained commit is seq 10; barrier 5 cannot
    # be served.  The error is raised before any device read.
    late = HistoricalDevice("late-device", 16, 6, initial_version=0,
                            initial=image_data(), start_seq=10)
    req = MirrorRequest("m", 5, {"late-device": late},
                        {"late-device": GROUP})
    try:
        MirrorScanner().snapshot_at(req)
    except SnapshotUnavailableError as exc:
        print(f"  status={exc.status}: {exc}")
        print(f"  reads served: {late.read_count} (history checked locally)")
    else:
        raise AssertionError("expected SnapshotUnavailableError")

    print()
    print("=== 5b. One device bus fault: no partial mirror published ===")
    good = HistoricalDevice("good", 16, 6, initial_version=2,
                            initial=image_data(), start_seq=1)
    faulty = HistoricalDevice(
        "faulty", 16, 6, initial_version=2, initial=image_data(), start_seq=1,
        faults={0: DeviceReadError("simulated bus fault on read 0")},
    )
    req = MirrorRequest("m", 5, {"good": good, "faulty": faulty},
                        {"good": GROUP, "faulty": GROUP})
    try:
        MirrorScanner(ScanConfig(max_read_words=2)).snapshot_at(req)
    except MultiDeviceReadError as exc:
        print(f"  status={exc.status}: {exc}")
        print("  no MirrorSnapshot returned; error carries diagnostics only")
    else:
        raise AssertionError("expected MultiDeviceReadError")

    print()
    print("demo finished: 1 committed snapshot, 1 torn read discarded,")
    print("1 rejection boundary, 1 barrier mirror, snapshot_unavailable and")
    print("a real device-read fault -- all computed live.")


if __name__ == "__main__":
    main()
