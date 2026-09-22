"""合成设备镜像：只暴露 read(start, count)，按确定性脚本在两次读取之间变更。

设备自身的寄存器状态对扫描器不可见；扫描器能观察到的只有 read() 返回的字。
版本寄存器遵循 seqlock 约定：提交前版本 +1（奇数=写入中），提交后再 +1
（偶数=稳定），16 位自然回绕（65534 -> 65535 -> 0）。

脚本按 read() 调用序号触发：events = {调用序号(1 起): [动作, ...]}，
动作在该次 read 取数之前依次执行，因此可以精确地把变更插到任意两次读取之间，
包括“某个 64 位字段的前两个字已读、后两个字未读”的跨块撕裂点。
"""

from __future__ import annotations

from typing import Callable, Protocol, Sequence, runtime_checkable

from .errors import DeviceProtocolError

MAX_READ_COUNT = 125
MAX_VERSION = 0xFFFF


@runtime_checkable
class RegisterDevice(Protocol):
    """设备镜像协议：read 返回恰含 count 个 16 位无符号整数的序列。"""

    def read(self, start: int, count: int) -> Sequence[int]: ...


class ScriptedRegisterDevice:
    """由初始映像和变更脚本驱动的离线设备。

    initial:  {地址: 16 位值}，未写入的地址读回 0
    events:   {第 k 次 read 调用(k 从 1 起): 该次读取前执行的动作列表}

    动作（tuple 指令，全部确定性、可重放）：
      ("begin", 版本地址)            版本寄存器 +1 并回绕（进入奇数）
      ("set",   地址, 值)            写一个 16 位寄存器
      ("setm",  {地址: 值})          批量写
      ("finish", 版本地址)           版本寄存器 +1 并回绕（回到偶数）
      ("commit", 版本地址, {地址: 值})  上面三步的完整提交
      ("call",  callable(device))    自定义钩子（接收设备本体）
    """

    def __init__(
        self,
        initial: dict[int, int] | None = None,
        events: dict[int, list[tuple]] | None = None,
    ) -> None:
        self._regs: dict[int, int] = {}
        self._events = dict(events or {})
        self.read_log: list[tuple[int, int]] = []  # [(start, count), ...]
        self.calls = 0
        if initial is not None:
            for addr, val in initial.items():
                self._write_reg(addr, val, where="initial")

    # ---- 供脚本/测试使用的直接操作（不经过 read 协议） ----
    def _write_reg(self, addr: int, val: int, *, where: str) -> None:
        if isinstance(addr, bool) or not isinstance(addr, int) or addr < 0:
            raise DeviceProtocolError(f"{where}: address must be int >= 0")
        if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 0xFFFF:
            raise DeviceProtocolError(
                f"{where}: register value at {addr} must be int in [0,65535], "
                f"got {val!r}"
            )
        self._regs[addr] = val

    def set(self, addr: int, value: int) -> None:
        self._write_reg(addr, value, where="set")

    def set_many(self, values: dict[int, int]) -> None:
        for addr, val in values.items():
            self._write_reg(addr, val, where="setm")

    def begin_write(self, version_addr: int) -> None:
        self._regs[version_addr] = (self._regs.get(version_addr, 0) + 1) & MAX_VERSION

    def finish_write(self, version_addr: int) -> None:
        self._regs[version_addr] = (self._regs.get(version_addr, 0) + 1) & MAX_VERSION

    def commit(self, version_addr: int, values: dict[int, int]) -> None:
        """一次完整的 seqlock 提交（奇 -> 数据 -> 偶），相对设备状态原子。"""
        self.begin_write(version_addr)
        self.set_many(values)
        self.finish_write(version_addr)

    def apply(self, action: tuple) -> None:
        kind = action[0]
        if kind == "begin":
            self.begin_write(action[1])
        elif kind == "set":
            self._write_reg(action[1], action[2], where="event set")
        elif kind == "setm":
            self.set_many(action[1])
        elif kind == "finish":
            self.finish_write(action[1])
        elif kind == "commit":
            self.commit(action[1], action[2])
        elif kind == "call":
            fn: Callable[["ScriptedRegisterDevice"], None] = action[1]
            fn(self)
        else:
            raise DeviceProtocolError(f"unknown script action: {action[0]!r}")

    # ---- 扫描器唯一可见的接口 ----
    def read(self, start: int, count: int) -> tuple[int, ...]:
        if isinstance(start, bool) or not isinstance(start, int):
            raise DeviceProtocolError("read: start must be int (bool rejected)")
        if isinstance(count, bool) or not isinstance(count, int):
            raise DeviceProtocolError("read: count must be int (bool rejected)")
        if start < 0:
            raise DeviceProtocolError(f"read: start must be >= 0, got {start}")
        if not 1 <= count <= MAX_READ_COUNT:
            raise DeviceProtocolError(
                f"read: count must be in [1,{MAX_READ_COUNT}], got {count}"
            )

        self.calls += 1
        for action in self._events.get(self.calls, ()):  # 本次读取前的变更
            self.apply(action)

        self.read_log.append((start, count))
        result = tuple(self._regs.get(start + i, 0) for i in range(count))
        # 合成设备始终自洽；该校验用于真实/第三方设备适配层
        for word in result:
            if isinstance(word, bool) or not isinstance(word, int) \
                    or not 0 <= word <= 0xFFFF:
                raise DeviceProtocolError(
                    f"read({start},{count}): word {word!r} outside [0,65535]"
                )
        return result
