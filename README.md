# regmirror — 工业寄存器镜像的原子扫描与版本门控位写

离线、纯标准库的可复用模块：对带版本寄存器的 16 位工业寄存器镜像做
**原子快照扫描**。设备在两次读取之间可能改写数据；扫描器用
"读版本 → 分段读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
`InconsistentScanError`。

写侧（本题增量）：**版本门控的批量位写**。写计划的每一项是
`(寄存器地址, 掩码, 新位值)`；`prepare_write_plan` 先在版本门控下
从同一版本镜像读出受影响寄存器，算出合并后的掩码、期望旧值与新值
（未选中的位原样保留），`commit_write_plan` 再把整批交给本地模拟设备的
`compare_version_and_write` 一次提交：版本变了就**整批拒绝、一个寄存器
都不写**，且不自动拿旧计划盲重试——调用方必须重新读取后再准备新计划。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` 脚本化变更）

## 环境准备（venv）

```bash
python3.14 -m venv .venv        # 或任何提供 3.14.7 的解释器
source .venv/bin/activate
# 无第三方依赖，无需 pip install
```

## 快速运行

```bash
python -m unittest discover -s tests -v   # 53 个测试
python demo.py                            # 扫描演示，约 0.1 秒
python demo_iteration.py                  # 位写计划演示，约 0.1 秒
```

## 接口概览

```python
from fractions import Fraction
from regmirror import (
    FieldSpec, RegisterGroup, ScanConfig, Scanner,
    DeviceMirror, Change,                # 合成设备镜像与变更脚本
    RegMirrorError, ConfigError, AddressError,
    DeviceReadError, InconsistentScanError,
)

energy = FieldSpec("energy", address=0, width=64)                 # u64
temp   = FieldSpec("temp", address=4, width=32, signed=True,
                   word_order="little", byte_order="big",
                   scale=Fraction(1, 10))                          # 精确比例
group  = RegisterGroup("boiler", version_address=6,
                       fields=(energy, temp))
snap   = Scanner(ScanConfig(max_attempts=4, max_read_words=16))
         .scan(device, [group])
v = snap["boiler"]["energy"]     # FieldValue: .raw, .value(Fraction), .value_float
```

### 公开数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec` | 字段布局：`name, address, width(32/64), signed, word_order, byte_order, scale(Fraction)` |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；字段区间不得互相重叠、不得覆盖版本寄存器 |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16) |
| `Snapshot` / `GroupSnapshot` | 全部组提交成功的结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field`；同一步的元组原子生效 |

### 写计划接口（本题增量）

| 名称 | 说明 |
|---|---|
| `BitWrite` | 一项位写：`address, mask, value`；`value` 在 `mask` 之外的位直接报错，`mask` 为 0 报错 |
| `prepare_write_plan(device, version_address, items)` | 版本门控读取 → `WritePlan`；同批重叠掩码若要求相反值，**在任何设备读取之前**抛 `ConfigError`；相容重叠自动合并 |
| `WritePlan` | `version_address, version, writes`；`writes[i]` 是 `PlannedWrite(address, mask, old, new)` |
| `commit_write_plan(device, plan)` | 一次原子提交，返回新版本；计划无论成败都被消费，不可重复提交 |
| `DeviceMirror.compare_version_and_write` | 设备侧原子原语：版本匹配才整批应用并把版本 +2（mod 2¹⁶），否则抛 `VersionConflictError` 且一字未写 |

```python
from regmirror import BitWrite, prepare_write_plan, commit_write_plan

plan = prepare_write_plan(dev, version_address=6, items=[
    BitWrite(3, 0x00FF, 0x0033),
    BitWrite(3, 0x000F, 0x0003),   # 相容重叠，合并为 mask=0x00FF
])
w = plan[3]                      # PlannedWrite: mask/old/new
commit_write_plan(dev, plan)     # 版本已变则整批拒绝，无部分写入
```

### 设备契约

设备对象需提供 `read(start, count) -> 16 位字序列` 和正整数 `size`。
扫描器对每次返回做长度与 0..65535 校验，违规抛 `DeviceReadError`。

## 扫描协议与版本语义

- 版本寄存器 16 位：**奇数 = 设备写入中，偶数 = 稳定**。每次提交 +2（mod 2¹⁶）。
- 一次尝试：读版本 → 按 `max_read_words` 分段读完全部数据寄存器 → 再读版本。
  两次版本**相等且为偶数**才提交该组；否则重试，最多 `max_attempts` 次。
- 任一组在预算内未提交 → 整个扫描抛 `InconsistentScanError`，
  异常只携带诊断用的原始版本字（`reasons: InconsistentRead`），
  **不携带任何解码后的字段值**——完整快照或什么都没有。
- **版本回绕契约**：版本相等 ⟹ 无提交，仅在"一次扫描期间设备提交
  不超过 **32767** 次"（`MAX_COMMITS_PER_SCAN = 2¹⁶/2 − 1`）时成立；
  32768 次提交会回绕到同一版本字造成混叠，这是部署契约，扫描器无法检测。

## 字节序/字序模型

- `byte_order="big"`：寄存器内高字节在前；`"little"`：低字节在前。
- `word_order="big"`：低地址寄存器是最高有效字；`"little"`：最低有效字。
- 先按字序排字、再按字节序拆字节，拼成 big-endian 字节串得到无符号原值，
  最后按 `signed` 做二进制补码解释。`encode_field` 为其逆运算并校验值域。

## 错误语义（区分类别）

| 异常 | 含义 |
|---|---|
| `ConfigError`(`ValueError`) | 配置本身非法，重试无意义（"证明无解"类）；**在任何设备读取之前**抛出 |
| `AddressError`(`ConfigError`) | 寄存器地址/区间越界 |
| `DeviceReadError` | 设备违反 `read` 契约（抛错、长度不符、非 16 位字） |
| `InconsistentScanError` | **重试预算耗尽**（与"配置不可能"明确分开）；不含部分值 |
| `VersionConflictError` | 提交时版本已变，整批拒绝（携带 `expected`/`actual`）；设备未写任何寄存器 |

输入校验总则：整数参数拒绝 `bool`；`scale` 接受 int/float/Fraction，
但 **NaN 与 ±Infinity 一律拒绝**（`ConfigError`），有限 float 按其二进制
值精确转换，不静默取整；字段/组重名、寄存器重叠等均定位报错，不静默纠正。

## 浮点误差口径

- `FieldValue.value` 是**精确**的 `Fraction`（`raw * scale`），无误差。
- `FieldValue.value_float` 是该有理数经 IEEE 754 最近舍入的双精度值，
  误差 ≤ 0.5 ulp（正确舍入）。需要无误差比较时请用 `.value`。

## 输入上限

- 寄存器地址空间：0..65535；镜像大小 1..65536 字。
- 字段宽度：32 或 64 位（2 或 4 字）；`max_read_words` ≤ 65536。
- `max_attempts` ≤ 1_000_000；一次扫描的提交次数契约上限 32767。

## 设计取舍

- **全量校验先于首次读取**：配置错误（`ConfigError`）绝不触碰设备，
  避免"失败留下未说明的部分变更"；扫描本身只读，不写设备。
- **分段读取固定顺序**（版本→数据升序分段→版本），使脚本化测试的
  变更点可精确对齐到某两次读取之间，撕裂场景确定可复现。
- **失败异常不携带解码值**：`InconsistentScanError` 只含版本字诊断，
  从类型上保证"无部分值"契约。
- **比例用 `Fraction` 而非 float**：缩放不引入舍入链，浮点仅在调用方
  显式取 `value_float` 时出现一次正确舍入。
- 只实现本题契约的版本门控子集，**不宣称**兼容 Modbus 等完整行业标准。

## 已知限制

- 版本混叠（≥32768 次提交/扫描）按契约不可检测，需部署侧保证。
- 单线程顺序扫描；组间无并行。
- 设备脚本（`DeviceMirror`）是确定性测试夹具，不是真实现场设备模型。
- 字段宽度仅 32/64 位，不含 16 位字段（可直接读单字，无需本模块）。
- `compare_version_and_write` 是**本地状态机**上的原子操作，只用于演示
  与测试协议语义，不要求、也不宣称真实设备具备该原子写命令。
- 写计划准备只做一次版本门控读取；门控失败抛 `InconsistentScanError`，
  由调用方决定是否重新准备（重新读取是允许的，盲重试旧计划不允许）。
- 写计划按单寄存器粒度合并/提交，不跨寄存器推断字段语义。

## 仓库结构

```
regmirror/           核心库（errors / device / fields / scanner / writer）
tests/               53 个 unittest（期望值手工计算，独立参考）
demo.py              扫描演示：正常提交 + 撕裂读丢弃 + 拒绝边界
demo_iteration.py    位写演示：合并计划 + 两客户端交错 + 冲突拒绝与重读
```
