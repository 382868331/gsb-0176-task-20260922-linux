# regmirror — 工业寄存器镜像的原子扫描

离线、纯标准库的可复用模块：对带版本寄存器的 16 位工业寄存器镜像做
**原子快照扫描**。设备在两次读取之间可能改写数据；扫描器用
"读版本 → 按计划读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
`InconsistentScanError`。

数据读取由一个**可检查的读取规划器**（`regmirror.planner`）事先算出：
在覆盖全部字段实际占用寄存器的前提下，按"数据 read 次数最少 →
额外寄存器数最少 → 块起点字典序最小"给出最优块序列，并遵守调用方给的
单次最大寄存器数、允许的额外（间隙）寄存器上限和禁读区间。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` 脚本化变更）

## 快速运行

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 70 个测试
./.venv/bin/python demo.py                            # 亚秒级，真实计算
```

## 接口概览

```python
from fractions import Fraction
from regmirror import (
    FieldSpec, RegisterGroup, ScanConfig, Scanner,
    ReadBlock, GroupPlan, ScanPlan,     # 可检查的读取计划
    plan_group, plan_scan,              # 不接设备也能规划
    DeviceMirror, Change,               # 合成设备镜像与变更脚本
    RegMirrorError, ConfigError, AddressError,
    DeviceReadError, InconsistentScanError,
)

energy = FieldSpec("energy", address=0, width=64)                 # u64
temp   = FieldSpec("temp", address=4, width=32, signed=True,
                   word_order="little", byte_order="big",
                   scale=Fraction(1, 10))                          # 精确比例
group  = RegisterGroup("boiler", version_address=6,
                       fields=(energy, temp),
                       forbidden_ranges=())   # 半开区间 [start, end)
snap   = Scanner(ScanConfig(max_attempts=4, max_read_words=16,
                            max_gap_words=8))
         .scan(device, [group])
v = snap["boiler"]["energy"]     # FieldValue: .raw, .value(Fraction), .value_float
```

### 读取规划（新增）

```python
scanner = Scanner(ScanConfig(max_read_words=7, max_gap_words=3))
plan = scanner.build_plan(device, [group])     # 纯规划，不触发任何 read
gp = plan["boiler"]
gp.blocks              # (ReadBlock(start, count), ...)，升序、互不重叠
gp.data_read_count     # 数据 read 次数（优化目标 1）
gp.extra_words         # 多读的非字段寄存器数（优化目标 2）
gp.block_starts        # 块起点元组（平局时字典序最小，优化目标 3）
gp.covered_words       # 字段实际占用的不同寄存器数（并集）
gp.attempt_reads       # 每次尝试的总 read 数 = 数据块数 + 2（两次版本读）

# 不接设备时也可以直接规划（size 即地址空间大小）：
gp = plan_group(group, size=16, max_read_words=7, max_gap_words=3)
sp = plan_scan([g1, g2], size=16, max_read_words=7, max_gap_words=3,
               forbidden_ranges=((10, 12),))   # 共享禁读区，对每组生效
```

规划器只决定**数据块**；每次尝试始终是"版本单字读 → 数据块 →
版本单字读"，版本检查**不会**并进任何数据块。版本地址若恰好落在
数据间隙里，默认只作为被忽略的间隙字读入；调用方可以把它列入
`forbidden_ranges` 显式禁止。

### 公开数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec` | 字段布局：`name, address, width(32/64), signed, word_order, byte_order, scale(Fraction)` |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；可选 `allow_overlap=True`（允许字段重叠，按寄存器并集覆盖）、`forbidden_ranges=((start,end),...)`（本组禁读半开区间） |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16)、`max_gap_words`(0..65535，默认 65535=任意间隙) |
| `ReadBlock` | 一次数据读：`start`、`count`（及 `end`/`as_tuple()`） |
| `GroupPlan` / `ScanPlan` | 每组/整次扫描的固定读取计划，可按名字索引 |
| `Snapshot` / `GroupSnapshot` | 全部组提交成功的结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field`；同一步的元组原子生效 |

### 设备契约

设备对象需提供 `read(start, count) -> 16 位字序列` 和正整数 `size`。
扫描器对每次返回做长度与 0..65535 校验，违规抛 `DeviceReadError`。

## 规划语义与最优性

输入约束：

- **最大单次寄存器数** `max_read_words`：任何数据块不得超过该长度。
  一个字段本身允许跨块（旧实现的分段语义不变，撕裂由版本门控拒绝）。
- **间隙上限** `max_gap_words`：一个块内允许包含的、无字段占用的
  寄存器数量上限。间隙两侧的字段只有在合并且不超过该上限时才会被
  放进同一次 read；`0` 表示绝不读取任何非字段寄存器。
- **禁读区间** `forbidden_ranges`：半开 `[start, end)`，自动校验并合并
  重叠/相邻区间。任何候选块与禁读区相交即不可用；字段或版本寄存器
  本身落在禁读区内属于构造上不可能，规划前抛 `ConfigError`。

在**全部字段实际占用寄存器（重叠字段取并集）都被覆盖**的前提下，
最优性依次为：

1. 数据 read 次数最少；
2. 总额外寄存器数最少（`Σ块长 − 覆盖寄存器数`）；
3. 块起点升序元组字典序最小。

其余固定规则：跨组**永不合并**，各组独立规划并各自做两次版本读；
字段字序、字节序与缩放比例不受规划影响。规划器对排序后的覆盖寄存器
做 O(m) 滑动窗口动态规划（m = 不同覆盖寄存器数）；测试在小地址空间上
枚举**所有**合法块组合（含块在覆盖寄存器外留余量的任意区间）独立核对
最优性，另含 400 组随机布局与万级寄存器的线性性能检查。

## 扫描协议与版本语义

- 版本寄存器 16 位：**奇数 = 设备写入中，偶数 = 稳定**。每次提交 +2（mod 2¹⁶）。
- 一次尝试：读版本 → 按计划升序读完所有数据块 → 再读版本。
  两次版本**相等且为偶数**才提交该组；否则该次尝试**整组作废**，
  用**同一份计划**从头重试（不复用上次尝试读到的任何部分块），
  最多 `max_attempts` 次。
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
| `ConfigError`(`ValueError`) | 配置本身非法，重试无意义（"证明无解"类，含字段/版本落入禁读洞、块长装不下任何合法切分）；**在任何设备读取之前**抛出 |
| `AddressError`(`ConfigError`) | 寄存器地址/区间越界 |
| `DeviceReadError` | 设备违反 `read` 契约（抛错、长度不符、非 16 位字） |
| `InconsistentScanError` | **重试预算耗尽**（与"配置不可能"明确分开）；不含部分值 |

输入校验总则：整数参数拒绝 `bool`；`scale` 接受 int/float/Fraction，
但 **NaN 与 ±Infinity 一律拒绝**（`ConfigError`），有限 float 按其二进制
值精确转换，不静默取整；字段/组重名、未显式 `allow_overlap` 的寄存器
重叠、非法禁读区间等均定位报错，不静默纠正。

## 浮点误差口径

- `FieldValue.value` 是**精确**的 `Fraction`（`raw * scale`），无误差。
- `FieldValue.value_float` 是该有理数经 IEEE 754 最近舍入的双精度值，
  误差 ≤ 0.5 ulp（正确舍入）。需要无误差比较时请用 `.value`。

## 输入上限

- 寄存器地址空间：0..65535；镜像大小 1..65536 字。
- 字段宽度：32 或 64 位（2 或 4 字）；`max_read_words` ≤ 65536；
  `max_gap_words` ≤ 65535；禁读区间端点满足 `0 ≤ start < end ≤ 65536`。
- `max_attempts` ≤ 1_000_000；一次扫描的提交次数契约上限 32767。

## 设计取舍

- **全量校验与规划先于首次读取**：配置错误（`ConfigError`）绝不触碰
  设备，避免"失败留下未说明的部分变更"；扫描本身只读，不写设备。
- **计划固定、尝试纯净**：计划在扫描前一次算出，失败尝试整组丢弃并
  按同一计划重跑，使脚本化测试的变更点可精确对齐到某两次读取之间，
  撕裂场景确定可复现，也杜绝了复用陈旧块的可能。
- **版本读绝不并入数据块**：两次单字版本读是门控协议的一部分，规划器
  只能合并数据寄存器，不能改变一致性检查结构。
- **失败异常不携带解码值**：`InconsistentScanError` 只含版本字诊断，
  从类型上保证"无部分值"契约。
- **比例用 `Fraction` 而非 float**：缩放不引入舍入链，浮点仅在调用方
  显式取 `value_float` 时出现一次正确舍入。
- 只实现本题契约的版本门控子集，**不宣称**兼容 Modbus 等完整行业标准。

## 已知限制

- 版本混叠（≥32768 次提交/扫描）按契约不可检测，需部署侧保证。
- 单线程顺序扫描；组间无并行，也不跨组合并读取。
- `max_gap_words` 只约束单块内的额外寄存器数；它不是一次扫描的总
  额外预算（最优性目标 2 才最小化总额外数）。
- 设备脚本（`DeviceMirror`）是确定性测试夹具，不是真实现场设备模型。
- 字段宽度仅 32/64 位，不含 16 位字段（可直接读单字，无需本模块）。

## 仓库结构

```
regmirror/           核心库（errors / device / fields / planner / scanner）
tests/               70 个 unittest（期望值手工计算 + 规划器枚举/随机独立核对）
demo.py              固定输入演示：规划合并 + 禁读洞分裂与整计划重试 + 拒绝边界
```
