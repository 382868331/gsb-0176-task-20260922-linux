# regmirror — 工业寄存器镜像的原子扫描

离线、纯标准库的可复用模块：对带版本寄存器的 16 位工业寄存器镜像做
**原子快照扫描**。设备在两次读取之间可能改写数据；扫描器用
"读版本 → 分段读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
`InconsistentScanError`。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 库版本：**1.1.0**（新增跨字位字段表解码，见下文"位级字段表"）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` 脚本化变更）

## 环境准备（venv）

仓库根目录已带 `.venv` 符号链接；如需重建：

```bash
python3.14 -m venv .venv        # 或 python3 -m venv .venv
source .venv/bin/activate       # 之后直接用 python 即可
```

venv 不是必需的——系统 Python 3.14.7 直接运行下列命令亦可，
模块只依赖标准库，无需 pip install。

## 快速运行

```bash
python -m unittest discover -s tests -v   # 54 个测试
python demo.py                            # 旧版原子扫描演示，约 0.1 秒
python demo_iteration.py                  # 位级字段表演示，约 0.1 秒
```

## 位级字段表（本轮新增）

调用方给一份字段表：每个字段声明**起始 bit、宽度、有无符号**，表级声明
16 位寄存器的**字序**；字段可横跨任意多个寄存器。解码入口返回每个值
**实际用到的寄存器及掩码**。

```python
from regmirror import BitFieldSpec, BitLayout, Scanner, decode_bitfields

layout = BitLayout(
    "boiler-bits",
    version_address=2,          # 16 位版本寄存器（奇=写入中，偶=稳定）
    base_address=4,             # 布局覆盖的连续寄存器起点
    word_order="little",        # 低地址寄存器是最低有效字
    fields=[
        BitFieldSpec("temp", start_bit=4, width=20, signed=True),
        BitFieldSpec("mode", start_bit=24, width=4),
    ],
)
snap = Scanner().scan_layout(device, layout)   # 一次版本门控扫描
v = snap["temp"]                # LayoutValue: .value, .registers
# v.registers == ((4, 0xFFF0), (5, 0x00FF))  实际用到的寄存器与掩码
```

### 位串模型（固定契约）

- 布局覆盖 `base_address` 起 `n_words` 个连续 16 位寄存器，
  `n_words` 由最高使用位推出。
- `word_order="little"`：低地址寄存器是最低有效字；`"big"` 反之。
  按字序拼成一条逻辑位串，bit 0 是最低有效字的最低位。
- 字段 `start_bit=s, width=w` 占逻辑位 `[s, s+w)`，无符号原值为
  `(logical >> s) & ((1<<w)-1)`；`signed=True` 按二进制补码解释。
- `LayoutValue.registers` 是按地址排序的 `(地址, 掩码)` 对，
  掩码标出该寄存器中属于此字段的位。

### union 与重叠

- 字段可用 `union="标签"` 显式声明为**同一 union 的不同视图**，
  同一 union 内的字段允许重叠（同一镜像的多个合法视图）。
- 其余任何位重叠都是非法布局，在**首次设备读取之前**抛 `ConfigError`。

### 原子性约定

- `Scanner.scan_layout` 的所有字段都从**同一次已验证的镜像**解码，
  绝不为某个字段临时读取第二份数据。
- 缺字（字数不符/非 16 位字）、世代冲突（版本门控重试耗尽）、
  非法布局都抛异常，**不产生任何部分解码结果**。
- 纯函数 `decode_bitfields(layout, words)` 可从已提交的字序列离线解码，
  同样全有或全无。

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
| `BitFieldSpec` | 位级字段：`name, start_bit, width(>=1), signed, union` |
| `BitLayout` | 位级字段表：`name, version_address, base_address, fields, word_order`；非 union 重叠在扫描前拒绝 |
| `LayoutValue` / `LayoutSnapshot` | 位级解码结果：`value` + `registers`（寄存器/掩码对） |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；字段区间不得互相重叠、不得覆盖版本寄存器 |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16) |
| `Snapshot` / `GroupSnapshot` | 全部组提交成功的结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field`；同一步的元组原子生效 |

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
- 位级字段表（`BitLayout`）无字节序概念：寄存器内位序固定为
  bit 0 = 该 16 位字的最低位；只有字序可选。

## 仓库结构

```
regmirror/           核心库（errors / device / fields / bitfields / scanner）
tests/               54 个 unittest（期望值手工计算，独立参考）
demo.py              旧版演示：正常提交 + 撕裂读丢弃 + 拒绝边界
demo_iteration.py    位级字段表演示：正常/边界/union/拒绝 + 旧接口保留
```
