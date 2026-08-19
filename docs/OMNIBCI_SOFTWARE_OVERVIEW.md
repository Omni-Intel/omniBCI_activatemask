# OmniBCI 八通道脑电采集软件概要

## 1. 软件定位

OmniBCI ADS1299 EEG 工作站是一套面向八通道脑电采集设备的 PC 上位机软件，配套 ESP32-C3 + ADS1299 固件使用。

软件目标：

- 采集 8 通道、24 bit、250 SPS EEG 数据。
- 支持 USB 串口和 BLE 无线连接。
- 保证原始数据记录优先于滤波、绘图和频谱分析。
- 提供实时波形、PSD、信号质量和传输诊断。
- 支持 BIN、CSV、BDF+、MNE FIF 等数据格式。
- 通过本地 API/SDK 向算法程序提供实时数据。
- 针对 Windows BLE 抖动和长时间采集进行连续性保护。

当前软件属于“以上位机为主要记录端”的方案，不包含设备端 TF 卡独立记录、IMU 采集、低电管理或固件 DFU 等可穿戴设备功能。

## 2. 总体架构

```text
ADS1299
  ↓ 8通道原始采样
ESP32-C3 固件
  ├─ USB 串口
  └─ BLE 可靠传输
          ↓
Transport Worker
          ↓
ADS 帧解析和时间线重建
  ├─ 原始数据环形缓冲
  ├─ 异步 BIN 记录
  ├─ 本地 raw 数据流
  └─ 实时滤波线程
          ├─ 波形显示
          ├─ PSD / Alpha 分析
          └─ 本地 filtered 数据流
```

软件将接收、记录、滤波和绘图分离。数据接收和 BIN 写盘不依赖 Qt 绘图刷新，避免界面拖动、PSD 计算或显卡性能影响采集。

主要代码文件：

- `ads1299_eeg_gui_native.py`：GUI、传输、解析、处理、记录和导出。
- `onmibci_stream.py`：本地 WebSocket 数据协议和服务器。
- `onmibci_sdk.py`：Python 客户端 SDK。
- `firmware/`：ESP32-C3 与 ADS1299 配套固件。

## 3. 采集参数

| 项目 | 当前设计 |
|---|---|
| EEG 通道数 | 8 |
| ADS 数据位宽 | 24 bit |
| 默认采样率 | 250 SPS |
| USB 波特率 | 921600 |
| 单帧大小 | 48 字节 |
| 数据单位 | 输入参考微伏（uV） |
| 支持传输 | USB 串口、BLE |
| 运行平台 | Windows + Python 3.12 |

## 4. 固件设计与功能

### 4.1 固件定位

设备端固件运行在 ESP32-C3 上，负责 ADS1299 初始化、采样、通道配置、诊断模式、USB 串口输出和 BLE 可靠传输。固件不进行 EEG 滤波、PSD 分析或 BDF 编码，这些功能由 PC 上位机完成。

固件侧的设计目标是：

- 按 ADS1299 DRDY 节拍持续读取 8 通道原始数据。
- 为每个采样分配连续的 32 bit 序号。
- 保留 ADS STATUS、采样状态、模式和丢帧诊断信息。
- 在 USB 和 BLE 两种输出方式下使用相同的 48 字节原始帧。
- 允许上位机动态修改参考模式、通道开关、PGA、BIAS 和 lead-off 配置。
- BLE 阻塞时优先保护 ADS 采集和已完成数据，不让通知发送反向阻塞采样任务。

### 4.2 固件版本

| 固件 | 参考方式 | 主要用途 |
|---|---|---|
| `ESP32C3_ADS1299_SRB1_BLE_V19` | 固定 SRB1 | 唯一正式固件；支持 USB、可靠 BLE、V1 握手和完整寄存器读回 |

当前分支只支持 SRB1-only 固件 V19，通信协议版本为 V1。SRB2、运行时参考切换及其他历史固件已经移除。

### 4.3 ADS1299 初始化与采样

固件启动后完成 GPIO、ADS1299、串口、BLE 和 FreeRTOS 队列初始化，并配置 ADS1299 为 250 SPS。默认启用 CH1～CH5，关闭 CH6～CH8，默认 PGA 为 24。

采样链路为：

```text
ADS1299 DRDY 下降沿
  → DRDY ISR 累计待处理采样
  → ADS 采集任务读取 STATUS + 8×24 bit 通道数据
  → 分配 sample sequence
  → 生成 48 字节 StreamFrame
  → frameQueue
  ├─ USB 串口输出
  └─ BLE 可靠块封装
```

固件会记录 DRDY 积压数量。如果同一轮唤醒时已经积累多个 DRDY，采样序号按实际缺口推进，使上位机能够看到真实的 sequence gap，而不是把缺失样本伪装为连续数据。

### 4.4 设备端任务划分

V18 BLE 固件使用三个主要任务：

| 任务 | 优先级 | 职责 |
|---|---:|---|
| ADS acquisition task | 5 | 响应 DRDY、读取 ADS1299、生成完整采样帧并写入 `frameQueue` |
| transport/pack task | 3 | 从 `frameQueue` 取帧，处理配置命令并封装可靠 BLE 数据块 |
| BLE TX task | 1 | 执行 DATA/STATUS Notify、发送新块和重传块 |

BLE Notify 被放在最低优先级的独立任务中。Windows 蓝牙栈暂停或 GATT Notify 阻塞时，采集任务和可靠块封装任务仍可继续运行。

### 4.5 原始数据帧

USB 和 BLE 解包后都使用相同的 48 字节 EEG 帧。主要字段包括：

| 字段 | 内容 |
|---|---|
| 同步头 | `0xA5 0x5A` |
| 协议/标志 | 帧版本及采集状态 |
| Sequence | 32 bit 连续采样序号 |
| ADS STATUS | ADS1299 原始 3 字节状态 |
| EEG 数据 | 8 通道，每通道 24 bit 二进制补码 |
| 读取时间 | 本次 ADS 读取耗时 |
| DRDY 状态 | DRDY 电平、积压和异常提示 |
| 运行模式 | EEG、BIAS off、内部短接或内部测试 |
| CRC | CRC16-CCITT-FALSE |

上位机使用同步头和 CRC 恢复帧边界，并结合 sequence、DRDY 状态和固件计数器判断数据缺口来源。

### 4.6 前端与诊断模式

固件支持以下 ADS1299 运行模式：

| 模式 | 用途 |
|---|---|
| EEG BIAS P+N | P/N 两侧共同加入 BIAS 计算的兼容模式 |
| EEG BIAS P-only | SRB1 推荐 EEG 模式，主要使用 `BIAS_SENSP` |
| EEG BIAS off | 关闭 BIAS 环路，用于排查共模反馈影响 |
| Input shorted | ADS1299 输入内部短接，用于测量系统底噪 |
| Internal test | ADS1299 内部测试信号，用于检查采集链路和增益 |
| Lead-off | 注入约 6 nA、31.25 Hz 信号，用于电极接触阻抗估算 |

进入内部短接或内部测试模式时，固件会关闭不适用的参考和 BIAS 配置。退出诊断模式后，由上位机恢复进入诊断前的 EEG 配置。

### 4.7 通道与寄存器控制

上位机通过二进制命令修改固件配置：

| 命令 | 功能 |
|---|---|
| `HELLO` | 返回固件版本 V19、协议版本 V1、能力标志和启动 ID |
| `GET_CONFIG` | 返回当前 ADS1299 完整寄存器快照和配置代数 |
| `SET_CONFIG` | 原子设置模式、启用掩码、BIAS、lead-off 和 8 通道增益，并读回确认 |

每个 BLE 请求包含消息类型、请求 ID、数据长度、内容和 CRC。固件通过独立 `RESPONSE` 特征返回相同请求 ID；上位机只有在完整寄存器读回与请求一致时才确认成功。

V19 固定保持所有 `CHnSET.SRB2=0`，正常 EEG 模式使用全局 `MISC1.SRB1`，BIAS 从 `BIAS_SENSP` 侧取样。

### 4.8 SRB1 接线边界

测量电极连接 `IN1P`～`IN8P`，公共参考连接 SRB1，原始极性为 `INxP - SRB1`，BIAS 电极连接 BIASOUT。V19 不支持 SRB2。

### 4.9 BLE 可靠传输

BLE DATA 使用 compact reliable protocol V2：

- 每个可靠块聚合 6 个 EEG 帧。
- 每块包含 session、block sequence、采样范围和 CRC16。
- 固件维护 384 个可靠块，250 SPS 下约可保留 9.2 秒数据。
- 上位机发送累计 ACK，释放已连续接收的数据块。
- 检测到缺块时，上位机发送范围 NACK 请求重传。
- 固件优先处理重传，再发送新的数据块。
- 丢失累计 ACK 时，固件会重发最旧的未确认块。
- V18 会忽略已经被后续 ACK 超越的过期 NACK，并裁剪仍有部分有效的请求范围。
- BLE MTU 过小时停止发送 EEG Notify，避免产生无法完整承载的数据包。

可靠保留环只能吸收有限时间的无线暂停，不等同于设备端永久存储。超过保留能力或 `frameQueue` 已满时，固件通过 sequence 和状态计数明确报告真实数据缺口。

### 4.10 STATUS 与固件诊断

BLE STATUS V4 使用独立的 76 字节状态包，配置响应不再占用 STATUS。状态内容包括：

- 当前采集模式、参考模式和通道配置。
- MTU、连接和流式状态。
- `frameQueue` 深度及最高负载。
- 设备端 queue drop 数量。
- DATA Notify 成功和失败数量。
- 命令队列丢弃数量。
- 可靠块生产、发送、确认、重传和保留数量。
- unknown NACK、协议错误和过期控制抑制数量。
- ADS STATUS、DRDY 和读取耗时信息。
- 当前配置代数 `config_generation`。

当前硬件没有电池电压采样或电量计接口，因此 STATUS 不提供电量百分比或电池电压。

上位机使用完整的 32 bit 固件计数器计算增量，避免较短的帧内提示字段回绕后造成错误归因。

### 4.11 固件职责边界

固件负责实时采样、原始帧生成、硬件配置和数据传输，不负责：

- 在设备端保存长期 EEG 文件。
- 对 EEG 执行带通、陷波或伪迹修复。
- 计算 PSD、Alpha 或脑电指标。
- 生成 CSV、BDF 或 FIF 文件。
- 保存自定义通道显示名称。
- 运行本地 WebSocket API 或算法模型。

这些功能由 PC 上位机完成。因此，USB/BLE 断开或 PC 退出后，固件只能保留可靠环容量范围内的临时数据，不能替代设备端 TF 卡记录。

## 5. 上位机主要功能

### 5.1 设备连接

- USB 串口和 BLE 两种连接模式。
- USB 默认波特率为 921600。
- BLE 支持扫描、连接、断线处理和自动恢复。
- USB/BLE 最终进入统一的帧解析、记录和显示链路。
- 支持读取固件状态及配置回读结果。

### 5.2 通道配置

每个通道支持：

- 独立启用或关闭。
- 独立 PGA 增益配置。
- 独立加入或退出 BIAS 共模反馈。
- SRB1/SRB2 参考模式配置。
- 独立波形纵轴范围。
- 自定义通道名称。

通道名称默认是 `CH1`～`CH8`，可在现有通道设置弹窗中修改。名称只对本次运行有效，限制为不重复、最长 16 字符的可打印 ASCII 名称，并写入导出的 BDF signal label。

### 5.3 实时显示与处理

- 八通道堆叠波形显示。
- 单通道放大查看。
- 每通道独立幅值范围。
- 时间窗口调整和离线时间导航。
- 默认 5～50 Hz 带通滤波。
- 50 Hz、100 Hz 工频及谐波陷波。
- 原始数据和滤波数据保持独立。
- Welch PSD 频谱分析。
- Alpha 频段及睁眼/闭眼 Alpha 对比。
- 饱和、平线、噪声和有效数据比例分析。

饱和值只在显示或滤波副本中屏蔽，原始 BIN 数据仍保存真实 ADS1299 数值。

### 5.4 阻抗检测

- 可选择需要检测的通道。
- 通过 ADS1299 lead-off 激励估算接触阻抗。
- 支持板载串联电阻补偿。
- 显示阻抗值和接触质量。
- 当前分档：`<10 kΩ` 良好、`10～50 kΩ` 可用、`>50 kΩ` 接触不良。
- 检测结束后恢复正常 EEG 配置。

### 5.5 数据记录

实时采集数据以原始 BIN 为主要记录格式：

- 每 60 秒自动切分一个文件。
- 250 SPS 下每个完整分段包含 15,000 个样本。
- 记录文件按会话建立目录。
- 每个分段生成 metadata sidecar。
- 每次会话生成 manifest。
- 写盘由独立线程执行，避免阻塞接收链路。
- 记录序列号、采集模式和配置等信息。

## 6. 数据导入与导出

软件支持：

- 导入原始 BIN 文件。
- 导入 BDF/BDF+ 文件。
- 离线查看波形、PSD 和质量指标。
- 导出 CSV。
- 导出 24 bit BDF+。
- 导出 MNE FIF。
- 打开 MNE 浏览器。
- 将无效帧写为 `BAD_frame` Annotation。
- 将用户事件写入 BDF+ Annotation。
- 将自定义通道名称写入 BDF signal header。

BDF/MNE 属于可选能力。未安装 `mne` 或 `pyedflib` 时，USB/BLE 采集和 BIN 记录仍可正常运行。

## 7. 本地 API 与 Python SDK

GUI 在本机提供 WebSocket 服务：

```text
127.0.0.1:8765
```

提供两类实时数据：

- `raw`：已经转换为微伏、未经显示滤波的原始 EEG。
- `filtered`：GUI 实时滤波线程的输出。

SDK 支持：

- `stream_raw()`：订阅原始数据。
- `stream_filtered()`：订阅滤波数据。
- `send_marker()`：发送事件标记。
- `stop_measurement()`：停止当前测量。
- `export_bdf()`：导出完整记录和事件。

数据批包含数值、样本序号、有效标记、采集模式和滤波配置代数。客户端处理不及时导致的数据丢弃会以显式 `GapEvent` 报告，不会静默丢数或伪造 EEG 样本。

## 8. 系统连续性与可靠性设计

当前 V19 版本重点解决长时间 BLE 采集连续性和设备状态确认问题：

- 固件采集/打包和 BLE 通知分离。
- 固件使用可靠保留环缓存待确认数据。
- 使用累计 ACK 和范围 NACK 请求重传。
- GUI 抑制已经过时的 ACK/NACK 控制。
- 根据 Windows 蓝牙适配器的通知间隔自适应重传时序。
- BLE 暂停时优先等待和恢复数据，不主动重启健康连接。
- 无法恢复的缺口会保留真实 sequence gap。
- USB 使用专用接收线程持续排空系统串口缓冲区。
- 绘图或 PSD 变慢不会主动暂停数据接收。

数据处理优先级为：

```text
传输接收
> 原始 BIN 记录
> 时间线重建
> 实时滤波
> 波形显示
> PSD 和质量分析
```

## 9. 上位机诊断能力

软件提供以下运行诊断：

- USB/BLE 接收队列负载。
- 串口驱动缓冲区状态。
- 样本序号缺口和丢失数量。
- 主机丢失与设备端丢失区分。
- BLE 重传、ACK/NACK 和保留环状态。
- BLE 通知间隔和适配器类型。
- 原始写盘队列及写入错误。
- 滤波积压和过期批次。
- 绘图刷新间隔。
- 饱和通道和饱和比例。
- PSD 跳过次数和计算状态。
- 固件 frameQueue、notify error 等状态字段。

## 10. 模块划分

| 模块 | 主要职责 |
|---|---|
| `AdsFrameParser` | 解析 ADS1299 帧、校验 CRC、转换通道数据 |
| `RingBuffer` | 保存原始和滤波时间线数据 |
| `SerialTransportWorker` | 持续排空 Windows 串口驱动缓冲区 |
| `BleTransportWorker` | BLE 通知接收、可靠重组、重传和状态维护 |
| `AsyncRawWriter` | 后台 BIN 分段写入和 metadata 管理 |
| `LiveFilterWorker` | 实时带通、陷波和连续状态滤波 |
| `PsdWorker` | 后台 PSD 和质量分析 |
| `MainWindow` | GUI、采集控制、绘图、导入导出和诊断 |
| `LocalStreamServer` | 本地 raw/filtered WebSocket 服务 |
| `LocalClient` | Python SDK 连接、订阅和控制请求 |

## 11. 系统边界

当前版本不包含：

- 设备端 TF 卡独立记录。
- IMU 数据采集和融合。
- 设备低电关机和掉电恢复。
- 固件 DFU 升级管理。
- 设备端 UTC 校时系统。
- 脱离 PC 的完整记录。
- 公网或局域网数据服务。
- 云端数据上传。

因此，当前项目重点是上位机实时采集、可视化、可靠传输、格式导出和算法接口，而不是完整的可穿戴设备嵌入式运行管理。

## 12. 验证与质量要求

当前测试覆盖：

- ADS 帧解析和 CRC 校验。
- 序列号缺口和时间线重建。
- 原始/滤波数据协议。
- WebSocket 订阅、广播和背压。
- SDK 数据解码和控制请求。
- Marker 到 BDF Annotation 的转换。
- BDF 通道名称写入和名称校验。

运行测试：

```powershell
uv run python -m unittest discover -s tests -v
```

可选导出依赖安装后，还可以执行真实 BDF+ 文件读写测试。

## 13. 运行入口

首次安装并运行：

```text
install_and_run.bat
```

后续运行：

```text
run.bat
```

直接使用 `uv`：

```powershell
uv sync
uv run python ads1299_eeg_gui_native.py
```
