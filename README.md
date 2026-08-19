# OmniBCI ADS1299 GUI

## 项目概览

OmniBCI 是面向 ADS1299 的原生 Python EEG 采集 GUI，支持 USB 串口与 BLE、实时滤波与绘图、PSD/质量分析，以及原始 BIN 数据保存。当前主线只支持 **SRB1-only 固件 V19 / 设备控制协议 V1**。

V19 延续并加固了长时间 BLE 连续性设计：当 Windows/BLE 通知链路出现抖动或拥塞时，固件的采集/打包路径不会被 BLE DATA notify 拖死，未成功提交的数据块也不会被误标为已发送。

关键数据路径为：

```text
ADS DRDY/read -> frameQueue -> reliable retention ring -> BLE DATA notify
```

其中前三段由高优先级采集/打包任务负责，BLE 通知由独立的低优先级任务负责。可靠保留环可保存 384 个六帧块，在 250 SPS 下约为 9.2 秒。

GUI 同时使用固件完整的 32 位 `queue_drop` 计数器进行丢帧归因，避免 8 位帧内提示在大规模突发丢帧时回绕而导致误判。

## 当前实现状态

### 已完成功能

- **采集与硬件控制**：USB 串口与 BLE 采集、V19/V1 版本握手、ADS1299 完整寄存器快照、通道开关/PGA/BIAS、内部短接、内部测试和电极阻抗检测。
- **SRB1-only**：GUI 与固件均移除 SRB2 切换入口；测量电极接 INxP，公共参考接 SRB1。
- **BLE 可靠传输**：六帧 compact block、384 块保留环、累计 ACK/NACK 修复、旧控制抑制、拥塞退避重试、512 帧采集队列和可分离 MCU/主机丢帧的 STATUS V5 诊断。
- **实时显示**：8 通道波形、自定义通道名、单通道视图、`A - B` 派生差分波形与 PSD、实时滤波、陷波、Welch PSD、阿尔法峰和信号质量指标。差分仅影响显示/分析，不篡改原始记录。
- **录制与导出**：每次采集写入一个连续 BIN，同时生成 manifest/sidecar 元数据；防止重复点击“开始”覆盖当前会话；支持 CSV、BDF+、FIF/MNE 导出和 BDF+ Annotation 事件标记。
- **本地数据 API**：`127.0.0.1:8765` WebSocket raw/filtered 数据流、Python SDK、客户端慢消费 `GapEvent`、实时 marker 和远程停止/导出。
- **日志与自恢复**：异步 JSONL 事件日志、滚动应用日志、GUI 按钮操作关联、BLE/STATUS/性能快照、渲染卡顿记录、主线程 hang dump、显示缓冲自适应和绘图连续卡顿时暂停 PSD。

### 2026-08-19 最新日志验证

分析对象为本地事件日志 `gui_20260819_181238_38380.jsonl`（日志文件本身不入库）。其中 BLE 连续采集约 4 分 30 秒，记录 67,560 帧 / 3,242,880 字节，与 `67,560 × 48` 完全一致。

| 指标 | 结果 | 判定 |
| --- | ---: | --- |
| ADS/主机序号缺口 | 0 | 未观察到波形缺口或真实掉包 |
| 固件 `frameQueue drop` / `notify error` | 0 / 0 | 上一版的主要丢帧原因已消失 |
| CRC / sync / decode error | 0 / 0 / 0 | 主机解码链正常 |
| NACK / 重传 / forced skip | 0 / 0 / 0 | 本次不需要修复包或跳过缺块 |
| reliable pending | 当前 0，峰值 1 | 可靠环无堆积 |
| 固件 retained / flight | 通常 3–8 / 2–8 | 距离 384 块容量上限很远 |
| missed DRDY / bad ADS STATUS | 0 / 0 | 采样时序和 SPI 帧对齐未观察到异常 |
| 最大 ADS 读取耗时 | 2,390 µs | 低于 250 SPS 的 4,000 µs 帧周期 |
| BLE Notify 长间隔 | p95 172 ms，最大 250 ms | Windows 批量交付仍存在，但无 >500 ms 事件且未造成丢帧 |
| GUI 运行期绘制延迟 | 偶发 109–125 ms | 屏幕刷新仍有轻微抖动，接收和 BIN 写盘未受影响 |

### 现有问题与风险

1. **最终 V19 整合版仍需硬件回归**：上述日志验证了相同的 BLE/SPI 连续性修复，但产生日志的运行发生在最终 V19/V1 握手代码合并前。当前 V19 固件已编译、Python 已通过测试，但仍应重新烧录并进行至少 30 分钟、最好整夜的实机回归。
2. **GUI 主线程仍有短暂停顿**：BLE 采集中记录到若干次 109–125 ms 绘制间隔；打开串口阶段有一次约 2.06 s 停顿，连接切换阶段有一次约 390 ms 停顿。这些暂未导致丢数据，但还可继续把串口打开/配置和更多绘图工作移出 Qt 主线程。
3. **软件 SPI 时序余量有限**：当前最大读取耗时 2.39 ms，低于 4 ms 帧周期，但在更重的 BLE/中断负载下余量不大。若后续出现 `missed_drdy` 或 `late_drdy` 持续增长，下一步应改用 ESP32-C3 硬件 SPI。
4. **存在 1 次未持续的 reliable protocol error**：该计数在一次 STATUS 中由 0 变为 1，之后未再增长，也没有引发 NACK、重传或数据缺口。暂按偶发/过时控制观察；如持续增长需记录原始 CONTROL 包类型和 CRC 失败位置。
5. **Windows BLE 设备名不可作为唯一身份**：日志中广播名仍可显示为截断的 `OmniBCI-`，Windows 缓存也可能保留多个同名记录。应以 MAC 地址和连接后 V19/V1 GATT 握手为最终判定。
6. **长时间稳定性尚未被这份日志证明**：4.5 分钟无丢包只能说明当前修复有效，不能替代 30 分钟、2 小时和整夜测试，也不能覆盖所有 Windows 蓝牙适配器。

## 安装与运行

### 首次运行

在新的 Windows 电脑上双击：

```text
install_and_run.bat
```

脚本会创建本地 `.venv`、安装实时采集所需依赖、校验环境并启动 GUI。脚本优先使用 64 位 Python 3.12 或 3.11。

### 后续运行

```text
run.bat
```

主程序文件为 `ads1299_eeg_gui_native.py`。为保证环境可复现，不建议通过 Windows 文件关联直接双击 `.py` 文件。

如果直接使用 uv，项目已经提交了 `uv.lock`，可在项目根目录执行：

```powershell
uv sync
uv run python ads1299_eeg_gui_native.py
```

同事的本地脚本也可以用同一个 uv 环境运行：

```powershell
uv run python your_model_script.py
```

### 可选导出能力

实时 USB/BLE 采集不依赖 `mne` 或 `pyedflib`。如需 BDF/FIF/MNE 导出，再运行：

```text
install_optional_exports.bat
```

可选包安装失败不会阻止实时采集、滤波、绘图和 BIN 记录。

## 本地实时数据 API 与 Python SDK

GUI 启动后会在本机 `127.0.0.1:8765` 提供 WebSocket 数据接口。接口只
允许本机连接，不提供公网或局域网监听。

当前提供两条逻辑数据流：

- `raw`：已经解析为微伏、但未经过 GUI 滤波的原始脑电；不是串口原始字节。
- `filtered`：GUI `LiveFilterWorker` 实时滤波后的数据。

### USB 与 BLE 两种采集模式

无论 GUI 当前选择有线 USB 串口还是 BLE，SDK 的连接和读取方式都相同。
两种模式都会进入 GUI 的公共帧解析、raw ring、实时滤波和 API 发布链路：

- USB 使用串口接收线程拿到的已解析帧；
- BLE 使用 BLE 接收线程重组后的时间线，BLE 可恢复的序号缺口会通过
  `sequence` 和 `valid=false` 保留下来。

`GapEvent` 表示 API 客户端自己的队列来不及读取而发生的丢批；它和 BLE/USB
采集时间线里的无效样本是两件事。模型应同时检查 `sequence`、`valid` 和
`GapEvent`，不要把缺失样本当成真实 EEG。

同事的 Python 脚本可以在项目目录中使用 SDK：

```python
from onmibci_sdk import GapEvent, connect_local

client = connect_local()
for item in client.stream_raw():
    if isinstance(item, GapEvent):
        print("stream gap:", item.dropped_samples)
        continue
    raw_eeg_uv = item.values  # shape: (samples, 8), dtype: float32
    # prediction = model.predict(raw_eeg_uv)
```

读取滤波数据时改用：

```python
for item in client.stream_filtered():
    if isinstance(item, GapEvent):
        continue
    filtered_eeg_uv = item.values
```

第一版 SDK 文件为 `onmibci_sdk.py`，协议模块为 `onmibci_stream.py`；如果
脚本不在项目目录中运行，需要将这两个文件一起放入脚本目录，或者把项目
根目录加入 `PYTHONPATH`。数据按批次实时发送，批次包含序列号、有效标记、
采集模式和滤波配置代数。

完整的同事接入说明见 [`docs/SDK_USAGE.md`](docs/SDK_USAGE.md)。

SDK 还支持实时事件标记和测量结束后的 BDF+ 导出：

```python
client.send_marker("stimulus_on", 1, sequence=12500, description="刺激开始")
client.stop_measurement()
result = client.export_bdf(r"D:\recordings\session_001.bdf")
```

事件会写入导出的 BDF+ Annotation 通道；导出功能需要 `uv sync --extra export`。

## 固件选择与兼容性

当前分支只保留并支持一套固件：

- `firmware/ESP32C3_ADS1299_SRB1_BLE_V19/ESP32C3_ADS1299_SRB1_BLE_V19.ino`

该固件为 **SRB1-only V19**，设备控制通信协议为 **V1**。GUI 只有在完成版本握手并取得 ADS1299 完整寄存器快照后，才确认设备已经就绪。

BLE 使用 `DATA`、`CONTROL`、`STATUS` 和独立的 `RESPONSE` 特征。配置请求包含事务 ID、长度和 CRC，固件通过 `RESPONSE` 返回相同事务 ID 和寄存器读回结果，避免旧 ACK 与周期状态包混淆。

STATUS V5 为 96 字节：保留 V19 配置代次，并追加 missed DRDY、late read、mutex busy、bad STATUS 和最大读取耗时，用于区分 MCU 采样缺口与主机 BLE 缺口。

### V19 固件任务配置

V19 使用以下连续性架构：

- ADS 采集任务：优先级 5
- `frameQueue` 到可靠保留环的打包任务：优先级 3
- BLE DATA notify 任务：优先级 1
- `frameQueue`：512 帧，约 2 秒短时缓冲
- 可靠保留容量：384 个六帧块，约 9.2 秒（250 SPS）
- BLE 拥塞时不推进未成功提交的块序号，而是有界退避后重试

SRB2、运行时参考切换和其他历史固件已经从本分支移除。

## 运行日志与卡死诊断

程序运行日志保存在项目或 EXE 同级的 `logs/onmibci.log`。日志文件达到
10 MB 后自动滚动，最多保留 10 个历史文件。通过“文件 → 打开日志目录”
可以直接打开该目录。

如果 GUI 主线程连续 5 秒没有响应，后台监测线程会自动生成
`logs/hang_YYYYMMDD_HHMMSS.log`，其中包含所有 Python 线程的调用栈。
排查卡死时应同时提供 `onmibci.log` 和对应时间的 `hang_*.log`。

电极接法：

```text
测量电极 CH1～CH8 -> IN1P～IN8P
公共参考电极      -> SRB1
BIAS 电极         -> BIASOUT
IN1N～IN8N        -> 不作为外部公共参考使用
```

正常 EEG 模式的测量极性为 `INxP - SRB1`。不要将 SRB1、SRB2、BIASOUT 或模拟地短接。

固件行为：

- 正常 EEG 模式：`MISC1(0x15) = 0x20`，全局开启 SRB1。
- 短路噪声和内部测试模式：`MISC1(0x15) = 0x00`。
- 所有 `CHnSET.SRB2` 位始终为 0；`A7` 命令中的 bit2 会被忽略。
- 默认启用 CH1～CH5、PGA 24、`BIAS_SENSP=0x1F`、`BIAS_SENSN=0x00`。
- GUI 与固件均按当前启用通道掩码过滤 `BIAS_SENSP`。
- `A7 CH GAIN FLAGS` 中 bit0 表示启用通道，bit1 表示加入 `BIAS_SENSP`，bit2 在本固件中忽略。

烧录时在 Arduino IDE 中选择 ESP32-C3，启用 `USB CDC On Boot`，打开 `firmware/ESP32C3_ADS1299_SRB1_BLE_V19/ESP32C3_ADS1299_SRB1_BLE_V19.ino`；串口波特率为 921600。

连接人体电极时必须使用电池供电和符合要求的电气隔离，不得让未隔离 USB 或市电设备形成到人体的导电通路。

## 实时处理与连续性设计

实时任务的优先顺序是：

1. 接收并保留每个传输帧；
2. 连续推进实时波形；
3. 执行滤波；
4. 仅在资源允许时更新 PSD/质量指标。

完整流水线为：

```text
BLE/USB receive -> raw BIN writer -> live filter -> waveform -> PSD
```

传输接收、BIN 写盘和滤波由相互独立的工作线程负责。PSD 严格保持 single-flight，不累计待执行任务。

### 饱和处理

- 原始 BIN、序列计数、滤波输入和 PSD 均保留真实、有限的满量程样本。
- 饱和值仅在绘制前的屏幕副本中裁剪到对应通道的可见 Y 范围。
- 该策略避免轨到轨跳变生成巨大的 QPainter 路径，同时不篡改记录与分析数据。
- 通道饱和本身不会暂停 PSD。

### BLE 显示连续性

- 不再因 BLE/滤波积压而主动暂停绘图。
- 不再等待抖动缓冲区完全回填后才恢复波形。
- 显示保留目标约为 0.72 秒，上限低于约 0.95 秒；储备较低时平滑减速，新数据到达后立即继续。
- 当无线链路确实停止交付样本且储备耗尽时，软件无法补造数据，但不会在数据恢复后额外制造一次“等待回填”的冻结。

### PSD 与绘制

- 实时 PSD 对最新 6 秒数据各执行一次 raw Welch 与 filtered Welch。
- 更复杂的重叠 4 秒质量窗口仅用于离线/捕获路径。
- PSD 使用私有的单线程 `QThreadPool`；上一次计算未结束时跳过下一次定时触发。
- 实时 PSD 刷新周期为 1.5 秒。
- 实时通道曲线启用 clip-to-view 与自动峰值降采样。

## 5. 诊断字段与问题定位

跨电脑或跨适配器比较时重点观察：

- `Serial worker`：当前/峰值主机 RAM 接收队列。
- `Serial gap/err`：串口读取线程最长交付间隔与读取错误。
- `Serial OS buffer`：请求的 1 MB Windows 驱动接收缓冲区是否生效。
- `BLE adapt`：学习到的适配器类别与 DATA notify p95 间隔。
- `BLE repair`：自适应 NACK 与 stall-reconnect 时序。
- `FW retained/flight`：固件保留块数量与在途压力。
- `FW frameQ/notifyErr`：固件 frameQueue 丢失与通知错误。
- `Stale ctrl suppressed`：GUI 本地抑制的过时 ACK/NACK 控制。
- `FW recent o/u/p`：从上次 STATUS 更新以来新增的 overflow / unknown NACK / protocol error。

判断提示：

- `FW retained` 增长而主机队列较低，通常指向无线链路或 Windows BLE 路径。
- `Serial worker` 增长通常表示主机处理滞后，而不是 USB 驱动已经丢失数据。
- BLE 与 Serial 的 sequence-gap 结论应分开解释。
- USB 电气噪声不属于 GUI 数据路径问题；若噪声随充电器、USB 隔离器或 BLE 电池供电方式明显变化，应检查 USB 地与共模耦合。

## Windows EXE 构建与分发

项目提供 PyInstaller Windows x64 ONEDIR 构建。ONEDIR 的优点包括启动较快、Windows Defender 解压开销较小、Qt/BLE DLL 加载更可靠、跨电脑诊断更容易。

构建后必须分发整个目录：

```text
dist\OmniBCI_V16\
```

目标电脑启动 `OmniBCI_V16.exe`，不需要安装 Python 或依赖包。不能只复制 EXE，因为相邻 `_internal` 目录包含 Python 运行时、Qt 与科学计算库。记录文件写入可执行文件旁的 `recordings\`。

当前构建资产仍沿用 `OmniBCI_V16` 命名，但代码与配套固件已进入 V19；正式发布前应统一 EXE、spec、输出目录和用户文档的版本命名。

## 版本变更摘要

### V19 — SRB1-only 握手、日志诊断与 BLE 掉包修复

- 只保留 SRB1 固件，增加 V19/V1 HELLO、事务 ID、CRC 和完整 ADS1299 寄存器快照。
- STATUS V5 扩展为 96 字节，保留 `config_generation` 并附加采集侧时序诊断。
- ADS 软件 SPI 读取整帧期间不再长时间禁用中断，避免每 4 ms 饿死 BLE 协议栈。
- BLE `notify` 拥塞失败不再误标已发送或推进块序号，改为有界退避与原块重试。
- 采集队列增加到 512 帧；GUI 控制 ACK/NACK 合并，拦截重复开始录制。
- 增加通道命名、`A - B` 差分显示/PSD、单 BIN 连续录制、JSONL 性能事件日志和卡顿自恢复。

### V18 — BLE 采集/发送隔离与 stale-NACK 热修复

- 将 BLE DATA/STATUS notify 从 `transportTask` 拆分到独立低优先级 `bleTxTask`。
- `transportTask` 只负责 `frameQueue -> compact block -> reliable retention`。
- 可靠槽仅在完整复制 payload 后才发布 `valid=true`；reset 时短暂停止 TX，避免并发重置。
- 停止流时保持 BLE TX 单一所有者。
- GUI 使用完整 32 位 `queue_drop` 进行 gap 归因，并区分 MCU frameQueue 丢失与主机可靠/解码丢失。
- GATT 写入前重新校验 NACK，合并重复/旧累计 ACK；ACK/NACK 使用 write-without-response，RESET 保持 write-with-response。
- 固件忽略旧会话控制以及已被累计 ACK 超越的 NACK，并会取消或裁剪排队中的过时修复范围。
- 诊断改用近期 STATUS 增量，历史一次性异常不再永久触发最高级别告警。

### V17 — 长时连续性

- Bleak DATA callback 只负责时间戳与入队，可靠重组/CRC/compact expansion 转移到专用解码线程。
- 单一缺失块在 2.4 秒或 96 个 pending blocks 后 fail-open，记录真实 ADS gap 并释放后续数据。
- watchdog 不再因 DATA 暂停推进而主动断开仍连接的 BLE 链路。
- 饱和隔离仅作用于显示副本，原始 BIN 和 ADS 序列保持不变。
- 可选 V17 固件将可靠保留扩展为 384 块，并降低流式 STATUS 无线流量。

### V16 Continuity Fix

- 满量程样本不再在实时滤波前转换为 NaN；只裁剪绘制副本。
- 实时 PSD 改为固定成本、私有单线程、严格 single-flight。
- 移除积压触发的主动绘图暂停与完整回填等待。
- 所有实时曲线使用 clip-to-view 与自动峰值降采样。
- 此次 continuity fix 本身是 GUI 更新；已有 V16 BLE 固件无需为该小版本重新烧录。

### V16 — 跨电脑适配

- 增加专用 `SerialTransportWorker`，持续排空 Windows 串口驱动并通过 RAM 队列交付 Qt。
- 配置 ACK 读取与正常 EEG 解析隔离，并报告 1 MB 串口 RX 缓冲区申请结果。
- BLE 时序按适配器 DATA notify 分布和 EWMA 自适应。
- 固件从固定 180 ms 重传超时切换为 ACK 驱动的 800–3000 ms 自适应超时。
- 使用本地 `.venv`，把 `mne`/`pyedflib` 移为可选导出依赖。
- 增加 PyInstaller 资源路径、spec、构建批处理及 GitHub Actions 构建流程。

### V15 录制链路（后续已改为单一连续 BIN）

- 每次采集持续写入单个原始 BIN：`MMDD_HHMM_ID.bin`，避免分钟边界的新文件创建和杀毒扫描干扰。
- GUI 每次启动会在 `logs/` 生成一个 JSONL 事件日志，关联按钮操作、固件状态、BLE Notify 长间隔、渲染卡顿、缓冲重同步和自动修复动作；日志由独立线程写入，不阻塞采集。
- 可靠 BLE 控制采用单一合并队列，只保留最新累计 ACK/NACK；缺块修复窗口为 6 秒，并在每次新连接配置后清理旧可靠会话，避免慢速 Windows GATT 写入形成控制任务风暴和过早跳块。
- 每次会话生成 manifest 与 sidecar metadata。
- BIN 写入、flush 与 JSON 元数据均在后台写盘线程中完成。

### V15 — 饱和稳定性修复（历史行为）

- 首次引入逐通道饱和检测与显示保护。
- 当时饱和样本会在实时滤波副本中转为 NaN，并可能暂停 PSD；这些行为已在 V16 Continuity Fix 中替换。
- 原始 ring buffer、序列统计和 BIN 始终保持不变。

### V14 — 低延迟

- BLE GUI poll：8 ms -> 4 ms；coalescing：16 帧 / 60 ms -> 8 帧 / 25 ms。
- 绘制：10 FPS -> 20 FPS；filter result poll：5 ms -> 3 ms。
- 无线显示储备从 2.0 秒降至约 0.65 秒，突发后光标最高以 1.75x 追赶。
- 屏幕历史超过约 1.12 秒时，仅重同步显示光标，不删除原始 BIN、序列统计或滤波数据。
- compact reliable BLE V2 协议保持与 V13 兼容。

## 文档整理说明

本 README 合并并去重了以下历史 Markdown 文件，合并完成后旧文件已从仓库工作树删除：

- `README.md`
- `README_V16.md`
- `README_CONTINUITY_FIX.md`
- `CHANGELOG.md`
- `firmware/ESP32C3_ADS1299_SRB1_BLE_V19/ESP32C3_ADS1299_SRB1_BLE_V19_README.md`

整理时同时修正了旧入口文档中指向不存在的 `CHANGELOG_V18.md` 与 `TEST_REPORT_V18.txt` 的提示；相关信息现已分别收录在本 README、`VERSION_AND_QUICK_START.txt`、`FIRMWARE_COMPATIBILITY.txt`、`EXE_BUILD_NOTES.txt` 和 `VALIDATION_REPORTS.txt` 中。
