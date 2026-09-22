# regscan —— 工业寄存器镜像的原子扫描

离线、确定性、仅标准库的可复用模块：对一块会随时变化的 16 位工业寄存器镜像做
**原子快照扫描**。每组数据带一个 16 位 seqlock 版本寄存器（写入中为奇数、稳定
为偶数），扫描器按「读版本 → 分段读数据 → 再读版本」的顺序取得一致窗口；两次
版本号相同且都为偶数才提交，否则有限次重试；耗尽后抛出**不含任何字段值**的
`InconsistentScanError`，调用方永远拿不到部分值。

## 运行环境

* Python **3.14.7**（语法仅用到 3.10+ 特性，目标按 3.14.7 验证）
* Linux 原生；**无需任何第三方依赖**，不联网、不需要硬件/数据库/中间件/云/Docker
* 所有示例数据由仓库内确定性代码实时生成，无 `sleep`、无预录结果

## 准确命令

在本目录（含 `regscan/`、`tests/`、`demo.py` 的目录）执行：

```bash
python -m unittest discover -s tests -v   # 必要测试（30 个）
python demo.py                            # 固定输入演示，实测 < 0.1 秒
```

## 快速上手

```python
from regscan import (
    FieldSpec, GroupSpec, RegisterScanner, ScriptedRegisterDevice,
)

group = GroupSpec(
    "line1",
    version_addr=0,                       # 版本寄存器地址（必须在数据窗口外）
    fields=(
        FieldSpec("temperature", 1, "s16", divisor=10),
        FieldSpec("flow", 2, "u32", word_order="low", divisor=100),
        FieldSpec("serial", 4, "u64", word_order="high"),
    ),
)
device = ScriptedRegisterDevice({0: 0, 1: 0xFF85, 2: 0xE240, 3: 1,
                                 4: 0x01, 5: 0x02, 6: 0x03, 7: 0x04})
snapshot = RegisterScanner(device, [group], read_block_size=4).scan()

snapshot.versions                    # {'line1': 0}
snapshot.fields["line1"]["temperature"].raw    # -123（精确 int）
snapshot.fields["line1"]["temperature"].value  # -12.3（binary64）
```

## 公开接口与数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec(name, address, data_type="u16", word_order="high", byte_order="big", divisor=1)` | 字段静态描述 |
| `GroupSpec(name, version_addr, fields, data_start=None, span=None)` | 数据组；窗口默认覆盖所有字段，版本寄存器必须在窗口外，字段区间不得重叠 |
| `FieldValue(raw, value)` | 快照字段：`raw` 为精确整数，`value` 为 `raw/divisor` 的 binary64 |
| `Snapshot(versions, fields)` | 完整快照：`{组名: 偶数版本}` 与 `{组名: {字段名: FieldValue}}`，全有或全无 |
| `RegisterScanner(device, groups, *, max_attempts=3, read_block_size=125)` | 扫描器；`scan() -> Snapshot`，对设备只读、无残留状态、可重复调用 |
| `ScriptedRegisterDevice(initial, events)` | 合成设备镜像，只暴露 `read(start, count)` |
| `RegisterDevice` | 结构类型（Protocol）：任何实现 `read` 的对象即可接入 |

**数据类型**：`u16/s16/u32/s32/u64/s64`，有符号按二进制补码解释。
**字序 `word_order`**：多寄存器字段中，`"high"` = 地址最小存最高有效字；
`"little-word"` 风格设备用 `"low"`（Modbus 常见）。
**字节序 `byte_order`**：每个 16 位寄存器内部 `"big"`/`"little"`。
**Fraction**：物理值 = 原始整数 / `divisor`（正整数）。

`ScriptedRegisterDevice` 的脚本事件按 `read()` 调用序号触发
（`events={k: [动作,...]}`，动作在第 k 次 read 取数前执行），动作：
`("begin", va)`、`("set", addr, val)`、`("setm", {addr: val})`、
`("finish", va)`、`("commit", va, {addr: val})`（奇→数据→偶的完整提交）、
`("call", fn)`。

## 错误语义

| 异常 | 语义 |
|---|---|
| `ConfigError`（含 `FieldSpecError` / `GroupSpecError`） | 配置非法，消息可定位到具体字段/组/参数 |
| `DeviceProtocolError` | 设备 `read` 返回长度不符或字不在 `[0,65535]` |
| `FieldOverflowError` | 一致窗口已取得，但换算值不是有限 binary64（正常 divisor≥1 不会触发） |
| `InconsistentScanError` | 重试预算耗尽。仅含 `failures={组:[原因...]}`（`"writing"` 奇数 / `"changed"` 偶数但不同）与 `attempts`，**不含任何寄存器字或字段值** |

* 所有整数参数拒绝 `bool`；数值输入拒绝 `NaN/Infinity`，非数字字符串不隐式转换。
* 非法输入在构造时即报错，不静默纠正（如 `encode_words` 遇 `值*divisor` 非整数直接拒绝）。
* 扫描失败不留任何本地部分状态，扫描器可立即复用。
* **预算耗尽 ≠ 证明无解**：`InconsistentScanError` 只表示「在 N 次尝试内未观察到
  一致窗口」，不断言设备永远不一致；设备静止后再次 `scan()` 可以成功。
  本模块不存在「证明无解」的搜索场景，不做此类断言。

## 输入上限（接口边界）

* 单次 `read`：`1 <= count <= 125`（合成设备与扫描器分段一致）；
* `max_attempts ∈ [1, 100]`，默认 3（有限重试，无「无限」配置）；
* `read_block_size ∈ [1, 125]`，默认 125；块大小只影响分段边界，不影响一致性结论；
* 寄存器字 `[0, 65535]`；版本号为 16 位无符号数；
* 组内字段区间互不重叠，字段名/组名唯一，版本寄存器不得落在字段/数据窗口内。

## 版本回绕契约

版本按 16 位自然回绕（65534 → 65535 奇 → 0 偶）。扫描器只比较两次采样字是否
相等，不假设单调递增，因此回绕正常支持。**契约前提**：同组两次版本采样之间，
设备提交次数少于 32768（半圈）；一次扫描期间最多 32767 次提交。超过半圈时偶数
版本号会在协议上重现（别名），这是 16 位 seqlock 的固有限制，任何纯从寄存器
观察的实现都无法区分。

## 浮点误差口径

* 权威值是 `FieldValue.raw`（精确整数）；`value` 仅为展示/计算便利，按
  IEEE-754 binary64 真除法 `raw / divisor` 计算（正确舍入）。
* 可精确表示的结果（如 1234/100 在本测试中用到的 binary64 取值）测试按**位精确
  相等**断言，不用宽容差掩盖边界错误。
* 一般地，对非零结果有相对误差上界
  `|value - 精确值| / |精确值| ≤ 2^-52`（整数→binary64 与一次真除法各最多
  0.5 ULP）；需要精确比例运算时应使用 `raw` 与 `divisor` 自行做整数/分数运算。
* 结果非有限数时模块拒绝输出（不产生 Infinity 快照）。

## 设计取舍

1. **版本号是唯一一致性依据**：跨读块的 64 位撕裂（新旧字拼接）即使数值“看起来
   合理”也只由版本不一致判定整体丢弃；绝不基于数据内容猜测或修补。
2. **先校验后解码**：失败尝试读到的数据不进入任何对象；异常体不带值，从结构上
   保证「无部分值」。
3. **逐组独立重试、整批提交**：所有组都一致才返回 `Snapshot`；任一组失败，已读
   组的数据也不出现在结果中。
4. **设备只通过 `read` 可见**：合成设备的直接写方法（`set/commit` 等）仅供脚本
   建模，扫描器不依赖它们，可替换为真实适配器。
5. 字序作用于寄存器之间，字节序作用于寄存器内部；这是本题的有限协议子集，
   **不宣称兼容任何完整行业标准**（不是完整 Modbus/PLC 协议栈）：无功能码、无
   字符串/位域/32 位交换等非本题字段类型。

## 已知限制

* 不处理跨组原子性（多组之间可能来自不同时刻；契约只保证组内一致）。
* 不检测「半圈内偶数版本别名」（见回绕契约），那是协议层不可观测问题。
* 不内置退避/定时：重试是立即再读；时序由设备脚本/真实设备决定。
* 无网络与真实硬件传输层，`RegisterDevice` 适配需自行保证 `read` 的原子性粒度
  与异常映射。

## 仓库结构

```
regscan/            本题库（errors/spec/device/scanner）
tests/test_spec.py    字段编解码、字序/字节序、Fraction、输入校验
tests/test_scanner.py 撕裂/字序/奇数持续/回绕/重试耗尽/无部分值/协议错误
demo.py             固定输入演示（正常结果 + 撕裂丢弃 + 重试耗尽异常）
```
