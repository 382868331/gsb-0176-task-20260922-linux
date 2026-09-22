"""regscan —— 工业寄存器镜像的原子扫描（离线、标准库）。

公开接口见各模块；常用名称从包根导入：

    from regscan import (
        FieldSpec, GroupSpec, FieldValue, Snapshot,
        RegisterScanner, ScriptedRegisterDevice,
        InconsistentScanError, ConfigError, DeviceProtocolError, FieldOverflowError,
    )
"""

from __future__ import annotations

from .device import MAX_READ_COUNT, RegisterDevice, ScriptedRegisterDevice
from .errors import (
    ConfigError,
    DeviceProtocolError,
    FieldOverflowError,
    FieldSpecError,
    GroupSpecError,
    InconsistentScanError,
    RegscanError,
)
from .scanner import DEFAULT_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS, MAX_SNAPSHOT_COMMITS, RegisterScanner
from .spec import (
    ByteOrder,
    DataType,
    FieldSpec,
    FieldValue,
    GroupSpec,
    Snapshot,
    WordOrder,
)

__all__ = [
    "ByteOrder",
    "ConfigError",
    "DataType",
    "DEFAULT_MAX_ATTEMPTS",
    "DeviceProtocolError",
    "FieldOverflowError",
    "FieldSpec",
    "FieldSpecError",
    "FieldValue",
    "GroupSpec",
    "GroupSpecError",
    "InconsistentScanError",
    "MAX_MAX_ATTEMPTS",
    "MAX_READ_COUNT",
    "MAX_SNAPSHOT_COMMITS",
    "RegisterDevice",
    "RegisterScanner",
    "RegscanError",
    "ScriptedRegisterDevice",
    "Snapshot",
    "WordOrder",
]
