"""固定输入演示：工业寄存器镜像的原子扫描。

运行：python demo.py（Python 3.14.7，仅标准库，约几十毫秒）

三段内容，全部由仓库内确定性代码实时计算，无 sleep、无预录结果、无网络：
  1. 正常扫描：s16/u32/u64 + 字序/字节序 + Fraction，打印完整快照；
  2. 撕裂读丢弃：64 位字段跨两个读块，设备在两块之间提交新版本，
     第 1 次尝试读到新旧拼接的畸形值，因版本号不一致被整体丢弃，重试成功；
  3. 拒绝边界：设备在每次尝试的两次版本采样之间持续提交，重试预算耗尽，
     抛出 InconsistentScanError（changed x3），异常不含任何部分值。
"""

from __future__ import annotations

import time

from regscan import (
    FieldSpec,
    GroupSpec,
    InconsistentScanError,
    RegisterScanner,
    ScriptedRegisterDevice,
)

VADDR = 0


def build_group() -> GroupSpec:
    return GroupSpec(
        "line1",
        version_addr=VADDR,
        fields=(
            FieldSpec("temperature_c", 1, "s16", divisor=10),          # -12.30 C
            FieldSpec("flow_lpm", 2, "u32", word_order="low",
                      byte_order="big", divisor=100),                  # 1234.56
            FieldSpec("serial", 4, "u64", word_order="high",
                      byte_order="big"),                               # 原始计数器
        ),
    )


def serial_image(value: int) -> dict[int, int]:
    """独立于被测核心的字面量布局：64 位 big-word 写入地址 4..7。"""
    return {4 + i: (value >> (48 - 16 * i)) & 0xFFFF for i in range(4)}


def initial_image(serial: int) -> dict[int, int]:
    image = {
        VADDR: 0,
        1: 0xFF85,                       # s16: -123
        2: 0xE240, 3: 0x0001,            # u32 little-word: 123456
    }
    image.update(serial_image(serial))
    return image


class LoggingDevice:
    """只做观测的 read 包装器：记录每次 read 返回的字（不参与算法）。"""

    def __init__(self, inner: ScriptedRegisterDevice) -> None:
        self.inner = inner
        self.calls: list[tuple[int, tuple[int, ...]]] = []

    def read(self, start: int, count: int) -> tuple[int, ...]:
        words = tuple(self.inner.read(start, count))
        self.calls.append((start, words))
        return words


def print_snapshot(snap) -> None:
    print(f"  版本寄存器(偶数)      = {snap.versions['line1']}")
    for name, fv in snap.fields["line1"].items():
        print(f"  {name:14s} raw={fv.raw!r:>22}  value={fv.value}")


def independently_join(words4: tuple[int, ...]) -> int:
    """演示侧独立拼字：纯移位，验证第 1 次尝试读到的撕裂值。"""
    value = 0
    for w in words4:
        value = (value << 16) | w
    return value


def main() -> None:
    t0 = time.perf_counter()

    print("=" * 68)
    print("1) 正常结果：静止设备的一次完整快照")
    print("=" * 68)
    old_serial = 0x0102030405060708
    dev = ScriptedRegisterDevice(initial_image(old_serial))
    snap = RegisterScanner(dev, [build_group()], read_block_size=4).scan()
    print_snapshot(snap)
    assert snap.fields["line1"]["temperature_c"].value == -12.3
    assert snap.fields["line1"]["flow_lpm"].value == 1234.56
    assert snap.fields["line1"]["serial"].raw == old_serial

    print()
    print("=" * 68)
    print("2) 撕裂读被丢弃：64 位 serial 跨读块，块间发生一次提交")
    print("=" * 68)
    new_serial = 0xA1A2A3A4A5A6A7A8
    raw_dev = ScriptedRegisterDevice(initial_image(old_serial))
    logged = LoggingDevice(raw_dev)
    # 读序(每次读4/3字一块)：1=v1 2=块1(地址1..4) 3=块2(地址5..7) 4=v2。
    # 在第 3 次 read 之前插入一次完整 seqlock 提交（版本 0->1->2）：
    # serial 的第一个字来自旧值、后三个字来自新值。
    raw_dev._events = {3: [("commit", VADDR, serial_image(new_serial))]}
    snap2 = RegisterScanner(logged, [build_group()],
                            max_attempts=3, read_block_size=4).scan()

    # 从第 1 次尝试的两个数据块（read 调用 2、3）独立还原撕裂窗口
    torn_window = logged.calls[1][1] + logged.calls[2][1]
    torn_serial = independently_join(torn_window[3:7])
    print(f"  第1次尝试 v1=0，块1读到 serial 首字 = 0x{logged.calls[1][1][3]:04X}（旧）")
    print(f"                块2读到 serial 后三字（新），拼接 = "
          f"0x{torn_serial:016X}")
    print(f"  第2次版本采样 v2=2 != v1=0 -> 该次尝试整体丢弃（changed）")
    print(f"  第2次尝试窗口静止，提交快照：")
    print_snapshot(snap2)
    assert torn_serial == 0x0102A3A4A5A6A7A8  # 新旧拼接的畸形值，从未进入快照
    assert snap2.fields["line1"]["serial"].raw == new_serial
    assert snap2.versions["line1"] == 2

    print()
    print("=" * 68)
    print("3) 拒绝边界：设备持续提交，3 次尝试全部不一致")
    print("=" * 68)
    busy = ScriptedRegisterDevice(initial_image(old_serial))
    # 每次尝试的第二次版本读之前（调用 4、8、12）都发生提交
    busy._events = {
        4: [("commit", VADDR, serial_image(0x1111111111111111))],
        8: [("commit", VADDR, serial_image(0x2222222222222222))],
        12: [("commit", VADDR, serial_image(0x3333333333333333))],
    }
    try:
        RegisterScanner(busy, [build_group()],
                        max_attempts=3, read_block_size=4).scan()
    except InconsistentScanError as exc:
        print(f"  捕获 {type(exc).__name__}: {exc}")
        print(f"  失败原因序列 = {exc.failures['line1']}")
        print(f"  尝试次数     = {exc.attempts['line1']}")
        print(f"  异常携带字段值? {hasattr(exc, 'fields')}（无部分值输出）")
    else:
        raise AssertionError("应当抛出 InconsistentScanError")

    print()
    print(f"完成，实际耗时 {1000 * (time.perf_counter() - t0):.1f} ms；"
          f"设备总 read 次数：场景1={dev.calls}，场景2={raw_dev.calls}，"
          f"场景3={busy.calls}")


if __name__ == "__main__":
    main()
