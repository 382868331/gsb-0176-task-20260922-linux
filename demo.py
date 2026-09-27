"""Offline demo of the regmirror atomic scanner.

Runs four real scans against a deterministic scripted device image:

1. A clean scan that commits on the first attempt (normal result).
2. A scan where the device rewrites a 64-bit field between two chunked
   reads -- the torn intermediate read is shown and discarded, the
   retry commits the consistent state.
3. A scan pauses, the device changes, and resume rejects old buffered words.
4. A failed update retains the previous complete, same-version image.

Everything is computed live from the scripted image; no sleeps, no
prerecorded output.  Expected runtime: well under 8 seconds.
"""

from fractions import Fraction

from regmirror import (
    Change,
    DeviceMirror,
    FieldSpec,
    PublishedImage,
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


class PauseController:
    def __init__(self, device, session, read_index):
        self._device = device
        self._session = session
        self._read_index = read_index
        self.size = device.size

    def read(self, start, count):
        words = self._device.read(start, count)
        if self._session is not None and self._device.read_count == self._read_index:
            self._session.request_pause()
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
    print("=== 3. Pause, device changes, resume rechecks version ===")
    dev = DeviceMirror(16, initial=image(version=2))
    scanner = Scanner(ScanConfig(max_attempts=3, max_read_words=2))
    controlled = PauseController(dev, None, read_index=2)
    session = scanner.start_session(controlled, [GROUP])
    controlled._session = session
    self_check = session.run()
    assert self_check is None and session.status == "paused"
    print("  paused after two reads; buffered words are unpublished")
    dev.apply_changes(
        [
            Change.set_version(6, 3),
            Change.set_words(0, ENERGY_V2 + TEMP_V2),
            Change.set_version(6, 4),
        ]
    )
    print("  device changed while paused; resume re-reads version first")
    resumed = session.run()
    show_snapshot(resumed)
    assert resumed["boiler"].version == 4

    print()
    print("=== 4. Failed update keeps the previous complete image ===")
    publisher = PublishedImage()
    result = publisher.try_update(
        Scanner(ScanConfig(max_attempts=2)), dev, [GROUP]
    )
    assert result.updated and publisher.current["boiler"].version == 4
    print(f"  published complete image at version "
          f"{publisher.current['boiler'].version}")

    unstable = DeviceMirror(16, initial=image(version=3))  # odd forever
    failed = publisher.try_update(
        Scanner(ScanConfig(max_attempts=4, max_read_words=2)),
        unstable,
        [GROUP],
    )
    print(f"  update failed: {failed.error}")
    print("  publisher still exposes only the previous complete snapshot:")
    print(f"  retained version = {publisher.current['boiler'].version}")
    assert not failed.updated and publisher.current["boiler"].version == 4

    print()
    print("demo finished: 1 committed snapshot, 1 torn read discarded,")
    print("1 rejection boundary retained the old image -- all computed live.")


if __name__ == "__main__":
    main()
