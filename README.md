# OmniBCI ADS1299 GUI

## 项目概览

OmniBCI 是面向 ADS1299 的原生 Python EEG 采集 GUI，支持 USB 串口与 BLE、实时滤波与绘图、PSD/质量分析，以及原始 BIN 数据保存。

V18 主要解决长时间 BLE 采集中的连续性问题：当 Windows/BLE 通知链路发生数秒暂停时，固件的采集/打包路径不再被 BLE DATA notify 阻塞。

关键数据路径为：

```text
ADS DRDY/read -> frameQueue -> reliable retention ring -> BLE DATA notify
```

其中前三段由高优先级采集/打包任务负责，BLE 通知由独立的低优先级任务负责。可靠保留环可保存 384 个六帧块，在 250 SPS 下约为 9.2 秒。

GUI 同时使用固件完整的 32 位 `queue_drop` 计数器进行丢帧归因，避免 8 位帧内提示在大规模突发丢帧时回绕而导致误判。

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

### 可选导出能力

实时 USB/BLE 采集不依赖 `mne` 或 `pyedflib`。如需 BDF/FIF/MNE 导出，再运行：

```text
install_optional_exports.bat
```

可选包安装失败不会阻止实时采集、滤波、绘图和 BIN 记录。

## 固件选择与兼容性

长时间 BLE 采集应使用项目内配套的 V18 固件：

- SRB1：`firmware/ESP32C3_ADS1299_SRB1_BLE/ESP32C3_ADS1299_SRB1_BLE.ino`
- SRB2：`firmware/ESP32C3_ADS1299_SRB2_BLE/ESP32C3_ADS1299_SRB2_BLE.ino`

V18 沿用 compact reliable BLE DATA protocol V2 / STATUS V4，因而仍可连接 V16/V17 可靠传输固件。但旧固件不具备 V18 的采集/发送任务隔离；要获得完整的长时连续性修复，应同时使用 V18 GUI 和 V18 固件。

V18 stale-NACK 热修复未改变 DATA/CONTROL 线格式，也未改变 72 字节 STATUS V4 布局。新固件会忽略已被后续累计 ACK 超越的过时 ACK/NACK 控制。旧固件可能留下历史 unknown-NACK 计数；V18 GUI 仅在该计数持续增长时将其作为当前异常。

### V18 固件任务配置

SRB1 与 SRB2 BLE 固件使用相同的连续性架构：

- ADS 采集任务：优先级 5
- `frameQueue` 到可靠保留环的打包任务：优先级 3
- BLE DATA notify 任务：优先级 1
- 可靠保留容量：384 个六帧块，约 9.2 秒（250 SPS）

### SRB1 P-only 参考固件

`firmware/ESP32C3_ADS1299_SRB1_PONLY_REFERENCE/` 是名称和行为固定的 SRB1 专用参考版本，不接受 SRB2 配置。

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

烧录时在 Arduino IDE 中选择 ESP32-C3，启用 `USB CDC On Boot`，打开 `ESP32C3_ADS1299_SRB1_PONLY_REFERENCE.ino`；串口波特率为 921600。

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

当前构建资产仍沿用 `OmniBCI_V16` 命名，但其 GUI 数据路径属于 V16 起建立、并延续到 V18 的跨电脑架构；正式发布前建议统一构建产物与当前 V18 的版本命名。

## 版本变更摘要

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

### V15 Split-BIN Fix

- 恢复每 60 秒自动切分原始 BIN：`MMDD_HHMM_ID_minuteNN.bin`。
- 250 SPS 下每个完整分段为 720,000 字节 / 15,000 帧。
- 每次会话生成 manifest，每个分段生成 sidecar metadata。
- 分段、轮换、flush 与 JSON 元数据均在后台写盘线程中完成。

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
- `firmware/ESP32C3_ADS1299_SRB1_BLE/ESP32C3_ADS1299_SRB1_BLE_README.md`
- `firmware/ESP32C3_ADS1299_SRB2_BLE/ESP32C3_ADS1299_SRB2_BLE_README.md`
- `firmware/ESP32C3_ADS1299_SRB1_PONLY_REFERENCE/ESP32C3_ADS1299_SRB1_PONLY_REFERENCE_README.md`

整理时同时修正了旧入口文档中指向不存在的 `CHANGELOG_V18.md` 与 `TEST_REPORT_V18.txt` 的提示；相关信息现已分别收录在本 README、`VERSION_AND_QUICK_START.txt`、`FIRMWARE_COMPATIBILITY.txt`、`EXE_BUILD_NOTES.txt` 和 `VALIDATION_REPORTS.txt` 中。
