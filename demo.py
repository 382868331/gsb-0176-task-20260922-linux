"""Offline demo of the regmirror atomic scanner and barrier mirror.

Runs five real scans against deterministic in-memory device images:

Legacy version-gated scanner (``Scanner.scan``):

1. A clean scan that commits on the first attempt (normal result).
2. A scan where the device rewrites a 64-bit field between two chunked
   reads -- the torn intermediate read is shown and discarded, the
   retry commits the consistent state.
3. A device whose version register stays odd forever -- the scan
   actually raises InconsistentScanError (rejection boundary).

New cross-device barrier mirror (``Scanner.mirror``):

4. Two devices with different commit counts, mirrored at one barrier:
   each device serves the last commit at or before the barrier, and a
   third device whose reader triggers more commits while the chunks are
   in flight still returns one frozen commit (no mixed fields).
5. Actual failures: a device that has no commit at the barrier
   (``snapshot_unavailable`` / history_insufficient) and a device whose
   requested commit was pruned from bounded history
   (history_pruned).  The whole mirror is not published.

Everything is computed live; no sleeps, no prerecorded output.
Expected runtime: well under 90 seconds.
"""

from fractions import Fraction

from regmirror import (
    Change,
    DeviceMirror,
    DeviceTarget,
    FieldSpec,
    HistoryDevice,
    InconsistentScanError,
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


# Turbine register map (image size 16):
#   9..10 rpm  u32, word big / byte big; 8 = version register
RPM = FieldSpec("rpm", 9, width=32)
TURBINE = RegisterGroup("turbine", version_address=8, fields=(RPM,))
# Hand-computed rpm images: raw 7 -> (0x0000, 0x0007); 9 -> that with 9.
RPM_V1 = (0x0000, 0x0007)
RPM_V2 = (0x0000, 0x0009)


def image(version):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = version
    return regs


def boiler_history(version_word=0):
    regs = dict(enumerate(ENERGY_V1 + TEMP_V1))
    regs[6] = version_word
    return HistoryDevice(16, initial=regs)


def turbine_history(version_word=0):
    return HistoryDevice(
        16, initial={8: version_word, 9: RPM_V1[0], 10: RPM_V1[1]}
    )


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
    print("=== 4. Barrier mirror across devices (normal result) ===")
    # Boiler history: seq0 = V1, then two commits seq1, seq2 = V2 state.
    boiler = boiler_history()
    boiler.stage(Change.set_words(0, ENERGY_V2 + TEMP_V2))
    boiler.commit_now(6)  # seq1
    boiler.stage(Change.set_words(0, ENERGY_V1 + TEMP_V1))
    boiler.commit_now(6)  # seq2
    # Turbine history: only seq0 = rpm 7.
    turbine = turbine_history()
    print(
        f"  histories: boiler latest={boiler.latest_sequence}, "
        f"turbine latest={turbine.latest_sequence}"
    )
    barrier = 2
    mirror = Scanner(ScanConfig(max_read_words=2)).mirror(
        [
            DeviceTarget("boiler", boiler, [GROUP]),
            DeviceTarget("turbine", turbine, [TURBINE]),
        ],
        barrier,
    )
    print(f"  requested barrier = {barrier}")
    print(f"  served sequences  = {mirror.sequences}")
    print(
        f"  boiler/energy   = {mirror['boiler']['boiler']['energy'].raw:#018x}"
        "  (seq2: V1 state again)"
    )
    print(
        f"  boiler/temp     = {mirror['boiler']['boiler']['temp'].value} "
        "(exact Fraction)"
    )
    print(
        f"  turbine/rpm     = {mirror['turbine']['turbine']['rpm'].raw}"
        "  (its last commit <= barrier is seq0)"
    )
    assert mirror.sequences == {"boiler": 2, "turbine": 0}

    print()
    print("=== 4b. Commits during the read cannot mix into one field ===")
    # A wrapper fires a fresh commit from inside every historical read;
    # even with history_depth=2 the pinned commit survives and every
    # decoded word comes from that one frozen image.
    busy = HistoryDevice(16, history_depth=2, initial=image(version=0))
    busy.stage(Change.set_words(0, ENERGY_V2 + TEMP_V2))
    busy.commit_now(6)  # seq1 = V2, the commit we will pin at barrier 1

    class CommittingReader:
        def __init__(self, d):
            self._d = d
            self.size = d.size
            self.fired = 0

        def resolve_barrier(self, b):
            return self._d.resolve_barrier(b)

        def pin(self, seq):
            self._d.pin(seq)

        def unpin(self, seq):
            self._d.unpin(seq)

        def retains(self, seq):
            return self._d.retains(seq)

        def read_at(self, seq, start, count):
            words = self._d.read_at(seq, start, count)
            self._d.stage(
                Change.set_words(0, ((0x90 + self.fired) & 0xFF, 0, 0, 0)
                                 + TEMP_V1)
            )
            self._d.commit_now(6)
            self.fired += 1
            return words

    reader = CommittingReader(busy)
    m4b = Scanner(ScanConfig(max_read_words=2)).mirror(
        [DeviceTarget("busy", reader, [GROUP])], barrier=1
    )
    print(
        f"  {reader.fired} newer commits fired mid-read; delivered seq="
        f"{m4b['busy'].sequence}, energy="
        f"{m4b['busy']['boiler']['energy'].raw:#018x} (frozen V2)"
    )
    assert m4b["busy"].sequence == 1
    assert m4b["busy"]["boiler"]["energy"].raw == 0x0A0B0C0D0E0F1011

    print()
    print("=== 5. snapshot_unavailable: insufficient and pruned ===")
    # 5a. A device with no history at all cannot serve barrier 0.
    empty = HistoryDevice(16, initial_committed=False)
    try:
        Scanner().mirror(
            [DeviceTarget("empty", empty, [GROUP])], barrier=0
        )
    except SnapshotUnavailableError as exc:
        print(f"  [no history]      {exc.code}: {exc}")
        assert exc.reason == "history_insufficient"
    else:
        raise AssertionError("expected SnapshotUnavailableError")

    # 5b. Bounded history (depth 2): after seq0..2, seq0 is pruned.  One
    # healthy device plus the pruned device in the same mirror: nothing
    # is published for either.
    healthy = boiler_history()
    pruned = HistoryDevice(16, history_depth=2, initial=image(version=0))
    for _ in range(3):
        pruned.stage(Change.set_words(0, ENERGY_V2 + TEMP_V2))
        pruned.commit_now(6)
    print(
        f"  pruned device retains {pruned.retained_sequences}; "
        "asking barrier 0 ..."
    )
    try:
        Scanner().mirror(
            [
                DeviceTarget("healthy", healthy, [GROUP]),
                DeviceTarget("pruned", pruned, [GROUP]),
            ],
            barrier=0,
        )
    except SnapshotUnavailableError as exc:
        print(f"  [pruned history]  {exc.code}: {exc}")
        print(
            "  whole mirror withheld (healthy device not published "
            "either); exception carries no field values"
        )
        assert exc.reason == "history_pruned"
        assert exc.device == "pruned"
        assert not hasattr(exc, "values")
    else:
        raise AssertionError("expected SnapshotUnavailableError")

    print()
    print("demo finished: legacy gate (1 commit, 1 torn read discarded,")
    print("1 odd-version rejection) + barrier mirror (1 cross-device")
    print("mirror, 1 mid-read no-mix proof, 2 actual unavailable errors).")


if __name__ == "__main__":
    main()
