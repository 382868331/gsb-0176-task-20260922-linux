"""异常层次：所有本库抛出的错误都继承 RegscanError。"""

from __future__ import annotations


class RegscanError(Exception):
    """regscan 所有异常的基类。"""


class ConfigError(RegscanError, ValueError):
    """字段/组/扫描器配置非法（可定位到具体参数）。"""


class FieldSpecError(ConfigError):
    """FieldSpec 参数非法。"""


class GroupSpecError(ConfigError):
    """GroupSpec 参数非法（含字段重叠、版本寄存器落在数据窗口内）。"""


class DeviceProtocolError(RegscanError):
    """设备镜像 read() 的返回违反 16 位寄存器协议（长度错误或值越界）。"""


class FieldOverflowError(RegscanError, OverflowError):
    """提交后的数据按 Fraction 换算得到有限 float 范围之外的结果。

    扫描本身是一致的（版本偶数且相同），但物理值无法以 binary64 有限表示，
    本库不输出 Infinity，因此整体拒绝、不产生部分快照。
    """


class InconsistentScanError(RegscanError):
    """有限次尝试内未获得一致快照。

    只携带诊断信息（组名、尝试次数、每次失败原因），**不携带任何寄存器字或
    已解码字段值**，保证调用方不可能拿到部分值。

    原因代码：
      * ``"writing"``：某次版本采样读到奇数（设备正在写入）；
      * ``"changed"``：两次版本采样均为偶数但不相等（两次读之间发生提交）。
    """

    def __init__(self, failures: dict[str, list[str]], attempts: dict[str, int]):
        if not failures:
            raise ValueError("InconsistentScanError requires at least one failed group")
        self.failures = dict(failures)
        self.attempts = dict(attempts)
        parts = []
        for name, reasons in self.failures.items():
            parts.append(
                f"group {name!r}: {attempts[name]} attempt(s), reasons={reasons}"
            )
        super().__init__("no consistent snapshot; " + "; ".join(parts))
