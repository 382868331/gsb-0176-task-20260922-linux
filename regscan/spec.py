"""公开数据结构：FieldSpec / GroupSpec / Snapshot，以及编解码核心。

寄存器模型（本题契约的有限子集，非完整行业标准）
--------------------------------------------------
* 寄存器为无符号 16 位，地址是非负整数；``read(start, count)`` 返回
  ``tuple[int, ...]``，长度恰为 count，每个值在 ``[0, 65535]``。
* 每个数据组有一个独立的 16 位 *版本寄存器* 与一段连续数据窗口
  ``[data_start, data_start + span)``，版本寄存器必须在窗口之外。
* 字段占据连续寄存器：

    ====== ========== ====
    类型   寄存器数   原始整数
    u16    1          0..65535
    s16    1          二进制补码
    u32    2          0..2^32-1
    s32    2          二进制补码
    u64    4          0..2^64-1
    s64    4          二进制补码
    ====== ========== ====

* 字序 ``word_order``：多寄存器字段中，``"high"`` 表示地址最小的寄存器
  存最高有效字（big-word）；``"low"`` 表示地址最小的存最低有效字（little-word，
  Modbus 常见接法）。
* 字节序 ``byte_order``：寄存器内部高/低字节，``"big"`` 为高字节在前，
  ``"little"`` 为低字节在前（常规 16 位寄存器按大端存字节时两种取值等价）。
* Fraction 比例：物理值 = 原始整数 / ``divisor``（divisor 为正整数）。

整数参数拒绝 bool；数值输入拒绝 NaN/Infinity。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, get_args

from .errors import FieldSpecError, FieldOverflowError, GroupSpecError

WordOrder = Literal["high", "low"]
ByteOrder = Literal["big", "little"]
DataType = Literal["u16", "s16", "u32", "s32", "u64", "s64"]

_WORD_ORDERS = frozenset(get_args(WordOrder))
_BYTE_ORDERS = frozenset(get_args(ByteOrder))
_DATA_TYPES = frozenset(get_args(DataType))

# 各类型占用的 16 位寄存器数与位宽
_WIDTH: dict[str, int] = {
    "u16": 1,
    "s16": 1,
    "u32": 2,
    "s32": 2,
    "u64": 4,
    "s64": 4,
}

MAX_REG = 0xFFFF
MAX_U64 = (1 << 64) - 1


def _check_int(value: object, what: str, *, min_value: int | None = None) -> int:
    """整数参数校验：拒绝 bool，拒绝非 int，可选下界。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldSpecError(f"{what} must be an int (bool rejected), got {value!r}")
    if min_value is not None and value < min_value:
        raise FieldSpecError(f"{what} must be >= {min_value}, got {value}")
    return value


def _check_finite_number(value: object, what: str) -> float:
    """数值输入校验：必须是 real number，拒绝 NaN/Infinity（bool 同样拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FieldSpecError(f"{what} must be a finite real number, got {value!r}")
    fv = float(value)
    if not math.isfinite(fv):
        raise FieldSpecError(f"{what} must be finite (NaN/Infinity rejected)")
    return fv


@dataclass(frozen=True)
class FieldSpec:
    """一个寄存器字段的静态描述。

    name       字段名（非空 str，同组内唯一）
    address    起始寄存器地址（>= 0）
    data_type  u16/s16/u32/s32/u64/s64
    word_order 多字字段的字序："high"（地址小=高字）或 "low"
    byte_order 寄存器内字节序："big"/"little"
    divisor    Fraction 比例分母，物理值 = 原始整数 / divisor（正整数）
    """

    name: str
    address: int
    data_type: DataType = "u16"
    word_order: WordOrder = "high"
    byte_order: ByteOrder = "big"
    divisor: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise FieldSpecError("field name must be a non-empty str")
        _check_int(self.address, f"field {self.name!r} address", min_value=0)
        if self.data_type not in _DATA_TYPES:
            raise FieldSpecError(
                f"field {self.name!r}: data_type must be one of "
                f"{sorted(_DATA_TYPES)}, got {self.data_type!r}"
            )
        if self.word_order not in _WORD_ORDERS:
            raise FieldSpecError(
                f"field {self.name!r}: word_order must be one of "
                f"{sorted(_WORD_ORDERS)}, got {self.word_order!r}"
            )
        if self.byte_order not in _BYTE_ORDERS:
            raise FieldSpecError(
                f"field {self.name!r}: byte_order must be one of "
                f"{sorted(_BYTE_ORDERS)}, got {self.byte_order!r}"
            )
        _check_int(self.divisor, f"field {self.name!r} divisor", min_value=1)

    @property
    def width(self) -> int:
        """占用寄存器数。"""
        return _WIDTH[self.data_type]

    @property
    def end_address(self) -> int:
        """最后一个寄存器地址（含）。"""
        return self.address + self.width - 1

    def decode_words(self, words: tuple[int, ...] | list[int]) -> tuple[int, float]:
        """把本字段的寄存器字解码为 ``(原始整数, 物理浮点值)``。

        words 从该字段地址开始、长度为 width；不做版本一致性判断，
        一致性由扫描器保证。
        """
        if len(words) != self.width:  # 内部接口，防御性检查
            raise FieldSpecError(
                f"field {self.name!r}: expected {self.width} words, got {len(words)}"
            )
        value = 0
        if self.word_order == "high":
            ordered = list(words)
        else:
            ordered = list(reversed(words))
        for reg in ordered:
            # 寄存器内字节序：常规硬件按 16 位单元寻址，字节序只在字内生效
            if not isinstance(reg, int) or isinstance(reg, bool) or not 0 <= reg <= MAX_REG:
                raise FieldSpecError(
                    f"field {self.name!r}: register word {reg!r} outside [0,65535]"
                )
            if self.byte_order == "big":
                word = reg
            else:
                word = ((reg & 0xFF) << 8) | ((reg >> 8) & 0xFF)
            value = (value << 16) | word

        if self.data_type[0] == "s":
            bits = self.width * 16
            sign = 1 << (bits - 1)
            value -= (value & sign) << 1

        scaled = value / self.divisor
        if not math.isfinite(scaled):
            # divisor >= 1、|value| <= 2^63-1，常规不会触发；属于不可表达的硬边界
            raise FieldOverflowError(
                f"field {self.name!r}: scaled value {value}/{self.divisor} "
                f"is not a finite float"
            )
        return value, scaled

    def encode_words(self, physical_value: float | int) -> tuple[int, ...]:
        """物理值 -> 寄存器字（供测试/演示中的合成设备独立建模使用）。

        物理值乘 divisor 后必须四舍五入到可表示整数（余数必须为 0），
        否则说明该值在此 Fraction 下不可精确表示，直接报错而不静默纠偏。
        """
        # 整数输入走精确有理路径；float 必须有限且拒绝 NaN/Infinity。
        # 大整数不得先转 float（64 位原始值会丢低位）。
        from fractions import Fraction

        if isinstance(physical_value, bool) or not isinstance(
            physical_value, (int, float)
        ):
            raise FieldSpecError(
                f"field {self.name!r} value must be a real number, "
                f"got {physical_value!r}"
            )
        if isinstance(physical_value, float) and not math.isfinite(physical_value):
            raise FieldSpecError(
                f"field {self.name!r} value must be finite (NaN/Infinity rejected)"
            )
        exact = Fraction(physical_value) * self.divisor
        if exact != int(exact):
            raise FieldSpecError(
                f"field {self.name!r}: value {physical_value!r} * divisor "
                f"{self.divisor} is not an integral register value"
            )
        value = int(exact)

        bits = self.width * 16
        lo, hi = (0, (1 << bits) - 1) if self.data_type[0] == "u" else (
            -(1 << (bits - 1)),
            (1 << (bits - 1)) - 1,
        )
        if not lo <= value <= hi:
            raise FieldSpecError(
                f"field {self.name!r}: raw value {value} out of range "
                f"[{lo}, {hi}] for {self.data_type}"
            )

        raw = value & ((1 << bits) - 1)
        words: list[int] = []
        for shift in range(bits - 16, -1, -16):  # 最高有效字在前
            word = (raw >> shift) & MAX_REG
            if self.byte_order == "little":
                word = ((word & 0xFF) << 8) | ((word >> 8) & 0xFF)
            words.append(word)
        if self.word_order == "low":
            words.reverse()
        return tuple(words)


@dataclass(frozen=True)
class GroupSpec:
    """一个受版本寄存器保护的数据组。

    name          组名（非空 str）
    version_addr  16 位版本寄存器地址
    fields        字段描述（字段寄存器区间互不重叠，且不得覆盖版本寄存器）
    data_start    数据窗口起点；默认取最小字段地址
    span          数据窗口寄存器数；默认覆盖到最大字段末地址
    """

    name: str
    version_addr: int
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    data_start: int | None = None
    span: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise GroupSpecError("group name must be a non-empty str")
        _check_int(self.version_addr, f"group {self.name!r} version_addr",
                   min_value=0)
        if not isinstance(self.fields, (tuple, list)) or not self.fields:
            raise GroupSpecError(
                f"group {self.name!r}: fields must be a non-empty tuple/list"
            )
        fields = tuple(self.fields)
        for f in fields:
            if not isinstance(f, FieldSpec):
                raise GroupSpecError(
                    f"group {self.name!r}: all fields must be FieldSpec, "
                    f"got {type(f).__name__}"
                )
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise GroupSpecError(
                f"group {self.name!r}: duplicate field names {names}"
            )

        start = min(f.address for f in fields)
        end = max(f.end_address for f in fields)
        _check_int(self.version_addr, f"group {self.name!r} version_addr")
        if start <= self.version_addr <= end:
            raise GroupSpecError(
                f"group {self.name!r}: version register {self.version_addr} "
                f"lies inside field window [{start}, {end}]"
            )

        if self.data_start is None:
            data_start = start
        else:
            data_start = _check_int(
                self.data_start, f"group {self.name!r} data_start", min_value=0
            )
            if data_start > start:
                raise GroupSpecError(
                    f"group {self.name!r}: data_start {data_start} does not "
                    f"cover first field at {start}"
                )
        if self.span is None:
            span = end - data_start + 1
        else:
            span = _check_int(self.span, f"group {self.name!r} span", min_value=1)
            if data_start + span - 1 < end:
                raise GroupSpecError(
                    f"group {self.name!r}: span {span} does not cover last "
                    f"field ending at {end}"
                )
        if data_start <= self.version_addr < data_start + span:
            raise GroupSpecError(
                f"group {self.name!r}: version register {self.version_addr} "
                f"lies inside data window [{data_start}, {data_start + span - 1}]"
            )

        # 字段区间互不重叠
        ordered = sorted(fields, key=lambda f: f.address)
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.address <= prev.end_address:
                raise GroupSpecError(
                    f"group {self.name!r}: fields {prev.name!r} and "
                    f"{nxt.name!r} overlap"
                )

        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "data_start", data_start)
        object.__setattr__(self, "span", span)


@dataclass(frozen=True)
class FieldValue:
    """快照中的单个字段：原始整数与按 Fraction 换算的物理值。"""

    raw: int
    value: float


@dataclass(frozen=True)
class Snapshot:
    """一次成功扫描的完整结果。

    versions  每组提交时读到的（偶数）版本号
    fields    {组名: {字段名: FieldValue}}，要么全有、要么整体失败
    """

    versions: dict[str, int]
    fields: dict[str, dict[str, FieldValue]]
