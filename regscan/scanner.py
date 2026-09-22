"""原子扫描器：版本号 seqlock 校验 + 分段读 + 全有或全无提交。

每个数据组的一次尝试（attempt）严格按以下读序列：

  1. read(version_addr, 1)  -> v1
  2. v1 为奇数：设备正在写入，本次失败（原因 "writing"），不读数据；
  3. v1 为偶数：按 read_block_size 分段读完整数据窗口；
  4. read(version_addr, 1)  -> v2
  5. v2 为奇数 -> "writing"；v2 != v1 -> "changed"（两次读之间发生提交，
     即使数据看起来“只差一点”也整体丢弃，跨读块的 64 位撕裂即在此被捕获）；
  6. v1 == v2 且均为偶数：该组一致，提交本组解码结果。

版本号按 16 位自然回绕：比较只看寄存器字是否相等，不假设单调递增，因此
回绕（65534 -> 0）正常支持。契约前提：同组两次版本采样之间设备提交次数
少于 32768（半圈），否则回绕别名（偶数号重现）在协议上不可区分——
这是 16 位 seqlock 的固有限制，不是本实现可弥补的。

全部组一致才返回完整 Snapshot；任一组在 max_attempts 内未成功则抛出
InconsistentScanError，异常不含任何字段值，调用方拿不到部分结果。
扫描器对设备只读，失败不留任何本地状态变更。
"""

from __future__ import annotations

from .device import MAX_READ_COUNT, RegisterDevice
from .errors import ConfigError, DeviceProtocolError, InconsistentScanError
from .spec import FieldValue, GroupSpec, Snapshot

DEFAULT_MAX_ATTEMPTS = 3
MAX_MAX_ATTEMPTS = 100  # 接口边界：有限重试，不允许“无限重试”配置
MAX_SNAPSHOT_COMMITS = 32767  # 契约：一次扫描期间设备最多提交次数


class RegisterScanner:
    """对一个 RegisterDevice、按若干 GroupSpec 做原子扫描。

    device           任何实现 read(start, count) 的设备镜像
    groups           GroupSpec 列表（组名唯一）
    max_attempts     每组最大尝试次数（含首次），[1, 100]
    read_block_size  每次 read 的最大寄存器数，[1, 125]
    """

    def __init__(
        self,
        device: RegisterDevice,
        groups: list[GroupSpec] | tuple[GroupSpec, ...],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        read_block_size: int = MAX_READ_COUNT,
    ) -> None:
        if not isinstance(device, RegisterDevice):
            raise ConfigError(
                "device must implement read(start, count) -> sequence of int"
            )
        if not isinstance(groups, (list, tuple)) or not groups:
            raise ConfigError("groups must be a non-empty list/tuple of GroupSpec")
        group_list = list(groups)
        for g in group_list:
            if not isinstance(g, GroupSpec):
                raise ConfigError(f"groups entry must be GroupSpec, got {type(g).__name__}")
        names = [g.name for g in group_list]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate group names: {names}")

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ConfigError("max_attempts must be int (bool rejected)")
        if not 1 <= max_attempts <= MAX_MAX_ATTEMPTS:
            raise ConfigError(
                f"max_attempts must be in [1,{MAX_MAX_ATTEMPTS}], got {max_attempts}"
            )
        if isinstance(read_block_size, bool) or not isinstance(read_block_size, int):
            raise ConfigError("read_block_size must be int (bool rejected)")
        if not 1 <= read_block_size <= MAX_READ_COUNT:
            raise ConfigError(
                f"read_block_size must be in [1,{MAX_READ_COUNT}], "
                f"got {read_block_size}"
            )

        self.device = device
        self.groups = group_list
        self.max_attempts = max_attempts
        self.read_block_size = read_block_size

    # ---- 内部：读窗口（分段拼接，保持每次 read 的实际边界可观测） ----
    def _read_window(self, start: int, span: int) -> tuple[int, ...]:
        chunks: list[tuple[int, ...]] = []
        remaining = span
        cursor = start
        while remaining > 0:
            size = min(self.read_block_size, remaining)
            chunk = self.device.read(cursor, size)
            if not isinstance(chunk, (tuple, list)) or len(chunk) != size:
                raise DeviceProtocolError(
                    f"read({cursor},{size}) must return {size} words, "
                    f"got {type(chunk).__name__} of length "
                    f"{len(chunk) if hasattr(chunk, '__len__') else '?'}"
                )
            words = tuple(chunk)
            for word in words:
                if isinstance(word, bool) or not isinstance(word, int) \
                        or not 0 <= word <= 0xFFFF:
                    raise DeviceProtocolError(
                        f"read({cursor},{size}): word {word!r} outside [0,65535]"
                    )
            chunks.append(words)
            cursor += size
            remaining -= size
        return tuple(w for chunk in chunks for w in chunk)

    @staticmethod
    def _read_version(device: RegisterDevice, addr: int) -> int:
        chunk = device.read(addr, 1)
        if not isinstance(chunk, (tuple, list)) or len(chunk) != 1:
            raise DeviceProtocolError(
                f"read({addr},1) must return exactly 1 word"
            )
        v = chunk[0]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFF:
            raise DeviceProtocolError(
                f"version word at {addr} outside [0,65535]: {v!r}"
            )
        return v

    def _scan_group(
        self, group: GroupSpec
    ) -> tuple[tuple[int, dict[str, FieldValue]] | None, list[str]]:
        """返回 (成功结果或 None, 每次尝试的失败原因)。"""
        reasons: list[str] = []
        for _ in range(self.max_attempts):
            v1 = self._read_version(self.device, group.version_addr)
            if v1 & 1:
                reasons.append("writing")
                continue

            data = self._read_window(group.data_start, group.span)

            v2 = self._read_version(self.device, group.version_addr)
            if v2 & 1:
                reasons.append("writing")
                continue
            if v2 != v1:
                reasons.append("changed")
                continue

            # 版本一致：此刻才解码，失败尝试的数据不产生任何字段值
            values: dict[str, FieldValue] = {}
            for f in group.fields:
                offset = f.address - group.data_start
                raw, scaled = f.decode_words(data[offset:offset + f.width])
                values[f.name] = FieldValue(raw, scaled)
            return (v1, values), reasons

        return None, reasons

    def scan(self) -> Snapshot:
        """执行一次全组扫描；成功返回完整 Snapshot，失败抛 InconsistentScanError。"""
        versions: dict[str, int] = {}
        fields: dict[str, dict[str, FieldValue]] = {}
        failures: dict[str, list[str]] = {}
        attempts: dict[str, int] = {}

        for group in self.groups:
            result, reasons = self._scan_group(group)
            if result is None:
                failures[group.name] = reasons
                attempts[group.name] = self.max_attempts
            else:
                versions[group.name], fields[group.name] = result

        if failures:
            # 不返回/不附带已成功组的任何数据：无部分值
            raise InconsistentScanError(failures, attempts)

        return Snapshot(versions=versions, fields=fields)
