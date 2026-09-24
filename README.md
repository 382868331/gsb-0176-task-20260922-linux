# regmirror — 工业寄存器镜像的原子扫描与跨设备屏障镜像

离线、纯标准库的可复用模块，提供两项能力：

1. **原子快照扫描**（v1.0）：对带版本寄存器的 16 位工业寄存器镜像，用
   "读版本 → 分段读数据 → 再读版本" 的门控协议，只在两次版本相同且为偶数
   时提交，否则有限重试，最终输出**完整快照**，或抛出**不含任何部分值**的
   `InconsistentScanError`。
2. **跨设备请求屏障镜像**（v1.1，本次迭代）：各合成设备保存有界、不可变的
   提交历史；给定屏障序列号，为**每台设备**返回其不晚于屏障的最后一次提交
   的全部字段。历史提交是冻结镜像，读取期间设备的新提交不可能混入同一字段；
   历史不足或已被裁剪时返回错误码 `snapshot_unavailable`；任一设备失败则
   **整面镜像不发布**。

- Python 版本：**3.14.7**（仅标准库，安装 Python 后无任何额外依赖）
- 平台：Linux 原生；核心、测试、演示全部离线，无需网络/账号/硬件/Docker
- 示例数据由仓库内确定性代码生成（`DeviceMirror` / `HistoryDevice` 脚本化变更）

## 快速运行

```bash
python -m unittest discover -s tests -v   # 63 个测试
python demo.py                            # 约 0.1 秒，真实计算
```

## 接口概览

```python
from fractions import Fraction
from regmirror import (
    FieldSpec, RegisterGroup, ScanConfig, Scanner,
    DeviceMirror, HistoryDevice, Change,      # 合成设备与变更脚本
    DeviceTarget, Mirror,                     # 跨设备屏障镜像
    RegMirrorError, ConfigError, AddressError,
    DeviceReadError, InconsistentScanError,
    SnapshotUnavailableError,
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

屏障镜像（新）：

```python
dev = HistoryDevice(16, history_depth=8, initial={...})
dev.stage(Change.set_words(0, words)); dev.commit_now(6)   # seq 1, 2, ...
mirror = Scanner(ScanConfig(max_read_words=16)).mirror(
    [DeviceTarget("boiler", dev, [group]), ...],
    barrier=5,                                                # 全局请求屏障
)
seq   = mirror["boiler"].sequence        # 该设备不晚于屏障的最后提交序号
words = mirror["boiler"]["boiler"]["energy"]   # DeviceSnapshot[group][field]
```

### 公开数据结构

| 名称 | 说明 |
|---|---|
| `FieldSpec` | 字段布局：`name, address, width(32/64), signed, word_order, byte_order, scale(Fraction)` |
| `FieldValue` | 解码结果：`raw`(int)、`value`(精确 `Fraction = raw*scale`)、`value_float`(float) |
| `RegisterGroup` | 一组字段 + 一个 16 位版本寄存器地址；字段区间不得互相重叠、不得覆盖版本寄存器 |
| `ScanConfig` | `max_attempts`(1..1_000_000，默认 4)、`max_read_words`(1..65536，默认 16) |
| `Snapshot` / `GroupSnapshot` | 门控扫描结果；按名字索引 |
| `DeviceMirror` | 确定性脚本化镜像：`read(start, count)`，脚本步骤 k 在第 k 次 read 之后生效 |
| `Change` | 脚本原语：`set_words` / `set_version` / `write_field` / `commit`；同一步的元组原子生效 |
| `HistoryDevice` | 带不可变提交历史的设备：`stage`/`commit_now`、`read_at(seq,...)`、`pin`/`unpin`、`resolve_barrier` |
| `DeviceTarget` | 屏障镜像的一个目标：`(name, history_device, groups)` |
| `DeviceSnapshot` | 一台设备在某个钉住提交上解码出的全部组；`.sequence` 为实际服务的提交序号 |
| `Mirror` | 一次屏障镜像的完整结果：`.barrier`、按设备名索引、`.sequences` 名字→序号 |

### 设备契约

门控扫描的设备对象需提供 `read(start, count) -> 16 位字序列` 和正整数
`size`。扫描器对每次返回做长度与 0..65535 校验，违规抛 `DeviceReadError`。

屏障镜像的设备对象需提供历史协议：`read_at(seq, start, count)`、
`resolve_barrier(barrier)`、`pin(seq)`/`unpin(seq)`、`retains(seq)` 与
正整数 `size`（`HistoryDevice` 已全部实现；外部设备可写适配器）。
`read_at` 的返回同样经过长度与字值校验。

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

## 跨设备请求屏障镜像（`Scanner.mirror`）

每个 `HistoryDevice` 把每次发布保存为**不可变冻结镜像**，序号从 0 起
稠密递增；普通 `read` 只看活动镜像，历史只能通过 `read_at(seq,...)` 读。

- **建历史**：`stage(change)` 改写活动镜像（未发布，对历史不可见）；
  `commit_now(*version_addresses)` 原子发布一个提交，被点名的版本寄存器
  在同一提交内各自 `+2 mod 2¹⁶`（暂存期间的奇数"写入中"字在发布时归正为
  下一个偶数）。`Change.commit(...)` 可放进脚本步骤里做同样的事。
- **屏障选择**：`mirror(targets, barrier)` 对每台设备解析
  **序号 ≤ barrier 的最后一次提交**（`bisect` 有序序列）；各设备提交计数
  独立，互不等待。barrier 大于最新提交时服务最新提交。
- **读取中不混字段**：解析到的序号先 `pin` 再分段读历史镜像；一次提交是
  一个冻结的字元组，之后设备再提交只会新增历史、替换活动镜像，**绝不会
  修改该元组**。被钉住的提交不受 `history_depth` 裁剪，读完在 `finally`
  中释放。因此无论读取期间设备提交多少次，每个字段都完整来自同一提交。
- **`snapshot_unavailable`**：屏障早于该设备任何提交
  （`reason="history_insufficient"`），或所需提交存在过但已被裁剪
  （`reason="history_pruned"`，异常带 `oldest`）。异常 `code` 属性是稳定
  字符串 `"snapshot_unavailable"`，供按码分支的调用方使用。
- **整面原子性**：任一设备不可用，`mirror` 直接抛出，不返回任何设备的
  部分结果，异常不携带解码值；已获取的 pin 在 `finally` 中全部释放。
  配置错误（`ConfigError`/`AddressError`）在任何历史访问之前抛出。

`HistoryDevice` 构造参数：`history_depth`（保留最近 N 次提交，`None` 为
不裁剪，默认 `None`）、`initial_committed`（默认 `True`，初始镜像作为
seq 0；`False` 表示设备开机后尚无任何提交）。

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
| `DeviceReadError` | 设备违反 `read`/`read_at` 契约（抛错、长度不符、非 16 位字） |
| `InconsistentScanError` | 门控扫描**重试预算耗尽**（与"配置不可能"明确分开）；不含部分值 |
| `SnapshotUnavailableError` | 屏障镜像：设备没有不晚于屏障的提交，或所需提交已被裁剪；`.code == "snapshot_unavailable"`，`.reason` 为 `history_insufficient`/`history_pruned` |

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
  `SnapshotUnavailableError` 只含屏障与原因，从类型上保证"无部分值"契约。
- **历史不可变 + pin 而非锁协议**：每次提交复制整幅镜像为不可变元组，
  历史读天然不混字段；pin 仅阻止有界窗口裁剪正在读的提交，不阻塞设备
  写入。单线程即可证明正确性，多线程下读者也不会拿到半提交。
- **比例用 `Fraction` 而非 float**：缩放不引入舍入链，浮点仅在调用方
  显式取 `value_float` 时出现一次正确舍入。
- 只实现本题契约的版本门控/屏障子集，**不宣称**兼容 Modbus 等完整行业标准。

## 已知限制

- 版本混叠（≥32768 次提交/扫描）按契约不可检测，需部署侧保证。
- 单线程顺序扫描；组间、设备间无并行 I/O（合成设备为内存镜像，无 I/O 成本）。
- `HistoryDevice` 每次提交复制整幅寄存器镜像，内存为 `depth × size` 字；
  不做差分/增量压缩。历史读不会推进变更脚本（只有活动 `read` 会）。
- 设备脚本（`DeviceMirror`/`HistoryDevice`）是确定性测试夹具，不是真实现场
  设备模型；接入真实设备时由调用方提供同样的 `read`/历史协议适配。
- 字段宽度仅 32/64 位，不含 16 位字段（可直接读单字，无需本模块）。
- 未实现版本回绕之外的时间戳屏障、取消协议或位域重映射（不在本题范围）。

## 仓库结构

```
regmirror/           核心库（errors / device / fields / scanner）
tests/               63 个 unittest（期望值手工计算/小规模枚举，独立参考）
demo.py              固定输入演示：门控正常/撕裂丢弃/拒绝 + 屏障正常/不混/不可用
```
