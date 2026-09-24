# regmirror — 工业寄存器镜像的原子扫描

离线、纯标准库的可复用模块，围绕带版本寄存器的 16 位工业寄存器镜像提供两层能力：

1. **单设备原子快照扫描**。设备在两次读取之间可能改写数据；扫描器用
   "读版本 → 分段读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
   时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
   `InconsistentScanError`。
2. **多设备屏障镜像（本次迭代新增）**。每台合成设备保留带全局提交序号的
   **有版本历史**；`MirrorScanner.snapshot_at` 按请求屏障返回每台设备
   "屏障前最后一次提交"的**全部字段**。屏障早于保留历史返回
   `snapshot_unavailable`；读取中的更新不能混入同一字段；任何一台设备失败，
   **整镜不发布**。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` / `HistoricalDevice` 脚本化变更）

## 快速运行

```bash
python -m unittest discover -s tests -v   # 71 个测试（旧 35 + 历史/屏障镜像 36）
python demo.py                            # 约 0.05 秒，真实计算
```

## 接口概览

```python
from fractions import Fraction
from regmirror import (
    FieldSpec, RegisterGroup, ScanConfig, Scanner,
    DeviceMirror, Change,                # 合成设备镜像与变更脚本
    # —— 屏障镜像（新增）——
    HistoricalDevice, DeviceCommit, CommitStore, CommitRecord,
    MirrorRequest, MirrorScanner,
    RegMirrorError, ConfigError, AddressError,
    DeviceReadError, InconsistentScanError,
    SnapshotUnavailableError, MultiDeviceReadError,
    InconsistentMirrorScanError,
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

# 屏障镜像：每台设备各自带提交历史，按全局屏障取"屏障前最后一次提交"
dev_a = HistoricalDevice("A", size=16, version_address=6,
                         initial_version=2, start_seq=1)
req  = MirrorRequest("plant", barrier=6,
                     devices={"A": dev_a}, groups={"A": group})
mirror = MirrorScanner(ScanConfig()).snapshot_at(req)
d = mirror["A"]        # DeviceSnapshot: .seq .version .source .attempts .values
d.source               # "history"（封存记录直接解码，0 次设备读）或 "live"
```

### 公开数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec` | 字段布局：`name, address, width(32/64), signed, word_order, byte_order, scale(Fraction)` |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；字段区间不得互相重叠、不得覆盖版本寄存器 |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16) |
| `Snapshot` / `GroupSnapshot` | 单设备全部组提交成功的结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field`；同一步的元组原子生效 |
| `CommitRecord` | **不可变**历史提交：`seq`（全局提交序号）、`version`（偶数稳定版本字）、`words`（提交时刻整镜像私有拷贝） |
| `CommitStore` | 只追加历史：`append(...)`、`latest_at_or_before(barrier)`、`oldest_seq/newest`；`max_records` 有界保留，超限丢弃最老记录 |
| `HistoricalDevice` | 带历史的合成设备（`DeviceMirror` 子类）：构造即写入初始提交；脚本支持 `DeviceCommit`；`faults={读序号: DeviceReadError}` 注入读故障 |
| `DeviceCommit.at(version, changes, seq=None)` | 一次原子稳定提交：改数据 → 置偶数版本字 → 追加整镜像 `CommitRecord`；`seq` 省略时自动递增 |
| `MirrorRequest` | `name, barrier(>=0), devices{id: dev}, groups{id: RegisterGroup}`；两台映射的键必须一致 |
| `MirrorScanner.snapshot_at(req)` | 返回 `MirrorSnapshot(status="published")`，或抛出下列失败异常之一 |
| `MirrorSnapshot` / `DeviceSnapshot` | 每台设备一条 `DeviceSnapshot`；按设备 id 索引；含 `seq/version/source/attempts/values` |

### 设备契约

单设备扫描的设备对象需提供 `read(start, count) -> 16 位字序列` 和正整数
`size`。屏障镜像在此之上还要求：`version_address: int`（须与该设备
`RegisterGroup.version_address` 相同）和 `history`（提供
`latest_at_or_before(barrier)` 与 `oldest_seq`，`HistoricalDevice`/
`CommitStore` 已满足）。扫描器对每次返回做长度与 0..65535 校验，违规抛
`DeviceReadError`（屏障镜像中聚合为 `MultiDeviceReadError`）。

## 屏障镜像语义（新增）

- **提交序号与屏障**：每次 `DeviceCommit` 带一个全局逻辑 `seq`（同一设备内
  严格递增，**允许不连续**——不同设备在不同全局序号上提交）。对屏障 B，
  每台设备选取**满足 `seq <= B` 的最后一次提交**。
- **全部字段**：结果包含该提交记录中该组每个字段的解码值。
- **读取中更新不混字段**：
  - 若选中提交**不是**设备最新提交，则它是历史中的**不可变整镜像记录**，
    直接从记录解码，**不发生任何设备读取**，设备正在进行的写入无从混入；
  - 若选中提交**就是**最新提交（屏障之后尚无新提交），执行常规门控读
    （版本 → 升序分段数据 → 版本），并额外要求读到的每个数据字与该历史
    记录**逐字相等**；撕裂字段会同时输在版本门控和内容比对上，然后重试。
    重试期间设备若越过屏障提交，扫描器改从封存的屏障记录解码。
- **`snapshot_unavailable`**：任一设备保留的历史中不存在 `seq <= barrier`
  的提交（屏障早于最老保留记录）时，在**任何设备读取之前**抛出
  `SnapshotUnavailableError`（`.status == "snapshot_unavailable"`，
  `.missing` 列出设备 id、屏障和该设备最老 seq）。
- **整镜 all-or-nothing**：任一设备违反读契约 →
  `MultiDeviceReadError`（`status="device_read_error"`）；任一设备重试预算
  耗尽 → `InconsistentMirrorScanError`（`status="mirror_inconsistent"`）。
  三种失败都不返回 `MirrorSnapshot`，异常只携带诊断（版本字、序号、设备
  id），**不含解码字段值**。

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
| `DeviceReadError` | 单设备扫描中设备违反 `read` 契约（抛错、长度不符、非 16 位字） |
| `InconsistentScanError` | 单设备扫描**重试预算耗尽**（与"配置不可能"明确分开）；不含部分值 |
| `SnapshotUnavailableError` | 屏障镜像：屏障早于某设备最老保留提交；`status="snapshot_unavailable"`，任何读取之前抛出 |
| `MultiDeviceReadError` | 屏障镜像：一台或多台设备违反 `read` 契约；`status="device_read_error"` |
| `InconsistentMirrorScanError` | 屏障镜像：一台或多台设备重试预算耗尽；`status="mirror_inconsistent"` |

三种屏障镜像失败都不返回镜像对象；诊断记录（`MissingHistory` /
`DeviceFailure` / `DeviceInconsistency`）只含设备 id、屏障、序号、版本字和
底层读错误，不含任何解码字段值。

输入校验总则：整数参数拒绝 `bool`；`scale` 接受 int/float/Fraction，
但 **NaN 与 ±Infinity 一律拒绝**（`ConfigError`），有限 float 按其二进制
值精确转换，不静默取整；字段/组/设备重名、寄存器重叠、设备与组的版本
寄存器不一致、devices/groups 键不匹配等均定位报错，不静默纠正。

## 浮点误差口径

- `FieldValue.value` 是**精确**的 `Fraction`（`raw * scale`），无误差。
- `FieldValue.value_float` 是该有理数经 IEEE 754 最近舍入的双精度值，
  误差 ≤ 0.5 ulp（正确舍入）。需要无误差比较时请用 `.value`。

## 输入上限

- 寄存器地址空间：0..65535；镜像大小 1..65536 字。
- 字段宽度：32 或 64 位（2 或 4 字）；`max_read_words` ≤ 65536。
- `max_attempts` ≤ 1_000_000；一次扫描的提交次数契约上限 32767。
- 屏障 `barrier >= 0`；提交 `seq >= 1` 且同一设备内严格递增（可跳号）；
  `CommitStore(max_records=...)` 有界保留，`None` 为不限。

## 设计取舍

- **全量校验先于首次读取**：配置错误（`ConfigError`）绝不触碰设备，
  避免"失败留下未说明的部分变更"；扫描本身只读，不写设备。屏障镜像的
  `snapshot_unavailable` 预检同样发生在任何设备读取之前。
- **分段读取固定顺序**（版本→数据升序分段→版本），使脚本化测试的
  变更点可精确对齐到某两次读取之间，撕裂场景确定可复现。
- **失败异常不携带解码值**：`InconsistentScanError` 只含版本字诊断；
  三种屏障镜像失败只含设备 id/屏障/序号/版本字/底层读错误，从类型上
  保证"无部分值、无部分镜像"契约。
- **历史提交是不可变整镜像快照**：封存记录直接解码、零设备读，既天然
  杜绝"读取中更新混入同一字段"，也让取旧屏障无需任何回绕协议。
- **live 读在版本门外再加内容逐字比对**：设备若违反"改数据必 bump 版本"
  协议（静默改写），版本门会被蒙混，内容比对仍会拒绝脏数据。
- **坏提交先校验后应用**：`DeviceCommit` 的多个变更先全部验证
  （越界/写版本寄存器/编码），通过后才落镜像并追加历史，不产生半提交。
- **比例用 `Fraction` 而非 float**：缩放不引入舍入链，浮点仅在调用方
  显式取 `value_float` 时出现一次正确舍入。
- 只实现本题契约的版本门控与屏障历史子集，**不宣称**兼容 Modbus 等
  完整行业标准；不做版本回绕恢复、取消协议或复杂位域重映射。

## 已知限制

- 版本混叠（≥32768 次提交/扫描）按契约不可检测，需部署侧保证。
- 单线程顺序扫描；设备间、组间无并行（任一失败仍保证整镜不发布）。
- 屏障镜像是**读时一致**：各设备在同一屏障下取各自最后提交，但设备之间
  不做分布式事务协调；屏障序号的全局语义由调用方/部署侧赋予。
- 有界历史在设备提交推进时可能于扫描中途丢弃目标记录，此时按
  `snapshot_unavailable` 处理（已覆盖测试）。
- 设备脚本（`DeviceMirror` / `HistoricalDevice`）是确定性测试夹具，
  不是真实现场设备模型。
- 字段宽度仅 32/64 位，不含 16 位字段（可直接读单字，无需本模块）。

## 仓库结构

```
regmirror/           核心库：errors / device / fields / scanner / mirror
tests/               71 个 unittest（旧 35 + 历史/屏障镜像 36；
                     期望值手工计算，独立参考）
demo.py              固定输入演示：正常提交、撕裂读丢弃、拒绝边界、
                     多设备屏障镜像、snapshot_unavailable、真实读故障
```
