# regmirror — 工业寄存器镜像的原子扫描

离线、纯标准库的可复用模块：对带版本寄存器的 16 位工业寄存器镜像做
**原子快照扫描**。设备在两次读取之间可能改写数据；扫描器用
"读版本 → 按规划块读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
`InconsistentScanError`。

数据读取前先由**可检查的读取规划器**把字段实际占用的寄存器合并成尽量少的
读取块：调用方给最大单次寄存器数、允许额外读取的间隙上限和禁止读取区间；
规划目标是先最少数据 read 次数，再最少额外寄存器数，最后块起点字典序。
规划结果（`ReadPlan`）可在扫描前取得并调用 `check()` 独立自检。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` 脚本化变更）

## 快速运行

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 72 个测试
./.venv/bin/python demo.py                            # 约 0.1 秒，真实计算
```

## 接口概览

```python
from fractions import Fraction
from regmirror import (
    FieldSpec, RegisterGroup, ScanConfig, Scanner,
    plan_reads, ReadPlan,                # 可检查的读取规划器
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
cfg    = ScanConfig(max_attempts=4, max_read_words=16,
                    max_gap_words=0, forbidden_ranges=((99, 100),))
plans  = Scanner(cfg).plan_scan(device, [group])  # 不读设备即可检查计划
plans["boiler"].check()                           # 独立合法性自检
snap   = Scanner(cfg).scan(device, [group])
v = snap["boiler"]["energy"]     # FieldValue: .raw, .value(Fraction), .value_float
```

### 公开数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec` | 字段布局：`name, address, width(32/64), signed, word_order, byte_order, scale(Fraction)` |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；字段**允许寄存器重叠**（占用区间合并读取），但不得覆盖版本寄存器 |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16)、`max_gap_words`(0..65535，默认 0)、`forbidden_ranges`（含端区间序列，默认空） |
| `ReadBlock` | 一个规划块：`start, count`（`.last` 为末地址） |
| `ReadPlan` | 可检查计划：`blocks`、`required/forbidden`、限额，以及 `read_count`、`extra_registers`、`starts`、`check()` |
| `plan_reads(required, *, max_read_words, max_gap_words, forbidden=())` | 纯规划入口，返回 `ReadPlan`；无解时抛 `ConfigError` |
| `check_read_plan(plan, required, *, ...)` | 独立校验任意 `ReadPlan` 的合法性 |
| `Snapshot` / `GroupSnapshot` | 全部组提交成功的结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field`；同一步的元组原子生效 |

### 设备契约

设备对象需提供 `read(start, count) -> 16 位字序列` 和正整数 `size`。
扫描器对每次返回做长度与 0..65535 校验，违规抛 `DeviceReadError`。

## 扫描协议与版本语义

- 版本寄存器 16 位：**奇数 = 设备写入中，偶数 = 稳定**。每次提交 +2（mod 2¹⁶）。
- 一次尝试：读版本 → 按**规划块**读完全部数据寄存器 → 再读版本。
  两次版本**相等且为偶数**才提交该组；否则该次尝试**整体作废**，按
  **同一个块计划**重新读一遍版本→数据→版本，**不复用**上次尝试已读的部分块。
  最多重试 `max_attempts` 次。
- **版本寄存器永远是独立的一次单字读，绝不并入数据块**；即使它紧挨着字段、
  即使调用方放宽间隙，规划器也自动把本组版本地址加入禁读集。
- 任一组在预算内未提交 → 整个扫描抛 `InconsistentScanError`，
  异常只携带诊断用的原始版本字（`reasons: InconsistentRead`），
  **不携带任何解码后的字段值**——完整快照或什么都没有。
- **版本回绕契约**：版本相等 ⟹ 无提交，仅在"一次扫描期间设备提交
  不超过 **32767** 次"（`MAX_COMMITS_PER_SCAN = 2¹⁶/2 − 1`）时成立；
  32768 次提交会回绕到同一版本字造成混叠，这是部署契约，扫描器无法检测。

## 读取规划器（可检查）

`plan_reads(required, *, max_read_words, max_gap_words, forbidden)` 在不触碰
设备的前提下生成 `ReadPlan`；`Scanner.plan_scan(device, groups)` 直接按各组
字段占用区间出计划。计划在首次设备读取前构建并通过 `ReadPlan.check()`
（即 `check_read_plan`）独立复核。

- **输入**
  - `required`：含端 `(start, end)` 区间序列，表示字段实际占用的寄存器；
    重叠或相邻区间自动合并，所以**重叠字段的共享寄存器只读一次**。
  - `max_read_words`：单个块的最大寄存器数（设备单次读取上限）。
  - `max_gap_words`：块内允许读取的**连续额外（非字段）寄存器**上限。
    `0` 表示不允许跨过任何间隙。
  - `forbidden`：含端禁读区间序列；构造时排序合并。
- **块合法性**：长度 1..`max_read_words`；不碰任何禁读寄存器；块在字段寄存器
  上起止（不向占用跨度之外延伸）；块内每段连续非字段寄存器不超过
  `max_gap_words`；块间不重叠、按地址升序；全部字段寄存器必须被覆盖。
- **最优性（固定顺序）**：① 数据 read 次数最少；② 额外寄存器总数
  （`sum(块长) − 字段寄存器数`）最少；③ 块起点元组字典序最小（确定性
  平局裁决，倾向把短块放前面：例如覆盖 0..4、上限 2 时起点为 `(0,1,3)`）。
- **无解即配置错误**：字段需要的寄存器落入禁读区间时，不存在合法计划，
  `plan_reads`/`scan` 在任何设备读取前抛 `ConfigError`（"证明无解"），
  与运行期重试耗尽的 `InconsistentScanError` 严格区分。
- **组隔离**：计划按组独立生成，块**绝不跨组合并**。
- 规划算法：禁读洞/超长间隙先把占用寄存器切成不可桥接簇，再对每簇做
  O(m) 动态规划（m = 字段寄存器数）。`tests/test_planner.py` 在 8 位小地址
  空间上枚举全部必需/禁读布局与限额组合（约 10 万例），用独立暴力实现
  核对上述三项最优性，且每个产出计划都过一遍 `check()`。

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

输入校验总则：整数参数拒绝 `bool`；`scale` 接受 int/float/Fraction，
但 **NaN 与 ±Infinity 一律拒绝**（`ConfigError`），有限 float 按其二进制
值精确转换，不静默取整；字段/组重名、字段覆盖版本寄存器、区间参数颠倒或
越界等均定位报错，不静默纠正。**字段寄存器区间允许重叠**（重叠部分合并
后只读一次）；真正"无解"的情形是字段寄存器落入禁读区。

## 浮点误差口径

- `FieldValue.value` 是**精确**的 `Fraction`（`raw * scale`），无误差。
- `FieldValue.value_float` 是该有理数经 IEEE 754 最近舍入的双精度值，
  误差 ≤ 0.5 ulp（正确舍入）。需要无误差比较时请用 `.value`。

## 输入上限

- 寄存器地址空间：0..65535；镜像大小 1..65536 字。
- 字段宽度：32 或 64 位（2 或 4 字）；`max_read_words` ≤ 65536。
- `max_gap_words` ∈ 0..65535；`forbidden_ranges` 的端点都在 0..65535。
- `max_attempts` ≤ 1_000_000；一次扫描的提交次数契约上限 32767。
- 规划器独立入口 `plan_reads` 的 `max_read_words` 上限为 65536。

## 设计取舍

- **全量校验先于首次读取**：配置错误（`ConfigError`）绝不触碰设备，
  避免"失败留下未说明的部分变更"；扫描本身只读，不写设备。
- **计划先建、自检、再用**：每次 `scan` 在任何设备读取前为每组构建计划并
  调用独立的 `check_read_plan` 复核；失败尝试整组丢弃，按同一计划重跑，
  不保留任何上次尝试的部分块。
- **读取顺序固定**（版本→规划块按地址升序→版本），使脚本化测试的变更点
  可精确对齐到某两次读取之间，撕裂场景确定可复现。
- **字段允许重叠、版本寄存器除外**：重叠是"多视图同寄存器"的常见需求，
  合并占用区间即可；但版本读必须自成一块，否则门控语义不成立。
- **失败异常不携带解码值**：`InconsistentScanError` 只含版本字诊断，
  从类型上保证"无部分值"契约。
- **比例用 `Fraction` 而非 float**：缩放不引入舍入链，浮点仅在调用方
  显式取 `value_float` 时出现一次正确舍入。
- 只实现本题契约的版本门控子集，**不宣称**兼容 Modbus 等完整行业标准。

## 已知限制

- 版本混叠（≥32768 次提交/扫描）按契约不可检测，需部署侧保证。
- 单线程顺序扫描；组间无并行，块也不跨组合并。
- 规划最优性按固定字典序平局裁决；不提供调用方自定义块布局的入口
  （可用 `check_read_plan` 校验外部构造的 `ReadPlan`，但 `Scanner` 始终
  使用内部最优计划）。
- 设备脚本（`DeviceMirror`）是确定性测试夹具，不是真实现场设备模型。
- 字段宽度仅 32/64 位，不含 16 位字段（可直接读单字，无需本模块）。

## 仓库结构

```
regmirror/           核心库（errors / device / fields / planner / scanner）
tests/               72 个 unittest（期望值手工计算；规划最优性含约 10 万例
                     小空间暴力枚举核对，独立参考不调用规划器内部）
demo.py              固定输入演示：正常提交、撕裂读丢弃、拒绝边界、
                     规划器重叠/间隙/禁读洞/无解
```
