# 全域智能 Omni-Intelligence ADS1299 EEG 工作站

这是一个面向 ADS1299 + ESP32-C3 的原生 Python 脑电采集、BIN 录制和离线回放工具。界面使用全域智能 Omni-Intelligence 白/黑/橙品牌视觉与紧凑的临床脑电走纸布局，保留真实微伏标定、原始数据诊断、滤波显示副本和 MNE 联动能力。

## 主要功能

- 解析固件输出的 48-byte 二进制帧并校验 CRC16-CCITT。
- 通过 921600 baud 串口实时采集 8 个 ADS1299 通道。
- 保存原始串口 BIN；显示处理不会改写原始数据。
- 打开历史 BIN，使用全程导航条、拖动、滚轮和方向键回放。
- 导入 BIN 后自动生成 MNE 交换 CSV 和双精度原生 FIF。
- 将原始微伏数据导出为 CSV。
- 一键构造 MNE `RawArray` 并打开 MNE 浏览器。
- 每个通道可独立设置 PGA 1/2/4/6/8/12/24，并按通道增益分别换算真实微伏。
- 全局参考模式可切换 SRB1 或 SRB2，并明确显示对应的电极接法。
- 实时帧通过 bit7 回报 SRB1 状态，GUI 会自动校正当前参考模式显示。
- 点击左侧通道可修改通道启用状态、PGA、BIAS 参与状态；SRB2 模式下还可逐通道控制 SRB2。
- SRB1 模式自动使用 `BIAS_SENSP`，SRB2 模式自动使用 `BIAS_SENSN`。
- 当前 PCB 使用 SRB1 公共参考且 SRB2 悬空，应保持选择 SRB1；GUI 同时兼容将 SRB2 正确引出的其他硬件。
- 提供原始 RMS、峰峰值、50 Hz 占比、丢帧、CRC 和 Alpha 质量诊断。

## GUI 布局

- 全域智能中英文 Logo 和应用图标。
- 白色紧凑参数栏，使用全域橙色作为交互强调色。
- 黑色专业信号画布，选中通道和导航窗口使用橙色高亮。
- 左侧固定 `CH1`–`CH8` 通道栏，点击即可修改对应的硬件参数。
- 中央黑色 EEG 走纸区，浅灰细波形、橙色选中波形、秒级主网格和 0.2 秒次网格。
- 底部采用 `HH:MM:SS` 时间刻度。
- 顶部全程时间导航条显示当前窗口位置。
- 右下角显示真实幅值和 1 秒校准标尺。
- 点击通道或波形可高亮；鼠标拖动平移、滚轮缩放，左右方向键整页翻动。

纵向通道间距仅用于排版，不作为幅值坐标显示。所有波形使用真实微伏值，统一受“灵敏度 uV/cm”控制，不使用自动 Y 轴缩放。每个通道只在显示副本中单独去直流。

## 默认显示参数

| 参数 | 默认值 |
| --- | --- |
| 时间窗 | 10 s |
| 走纸速度 | 30 mm/s |
| 灵敏度 | 100 uV/cm |
| 高通 | 5 Hz |
| 低通 | 50 Hz |
| 工频及谐波陷波 | 50 Hz + 100 Hz（Q=30） |

默认显示链为 5–50 Hz 带通，再级联 50 Hz 和 100 Hz 陷波。在 250 SPS
采样率下，模拟 150 Hz 分量会混叠到 100 Hz，因此由 100 Hz 陷波共同抑制。
同一滤波链同时用于实时显示和导入 BIN 后的离线显示；离线 BIN 使用双向零相位
滤波。滤波只作用于显示副本，BIN 文件始终保存原始串口字节。关闭“滤波后”后
显示逐通道去直流的原始微伏数据。

## 安装与运行

要求 Python 3.10 或更高版本。

首次运行：

```bat
install_and_run.bat
```

依赖已经安装时：

```bat
run.bat
```

也可以手动执行：

```powershell
py -3 -m pip install -r requirements.txt
py -3 ads1299_eeg_gui_native.py
```

主要依赖：

- PySide6
- pyqtgraph
- NumPy
- SciPy
- pyserial
- MNE（仅“打开 MNE 浏览器”功能需要）

## 数据帧格式

| 偏移 | 内容 |
| --- | --- |
| 0–1 | 同步字 `A5 5A` |
| 2 | 协议版本 `1` |
| 3 | 帧类型 `1` |
| 4–7 | uint32 little-endian 序号 |
| 12–14 | ADS1299 STATUS |
| 15 | 状态标志 |
| 16–39 | CH1–CH8，signed 24-bit big-endian |
| 40–45 | 采集诊断字段 |
| 46–47 | CRC16-CCITT little-endian |

默认采样率为 250 Hz。微伏换算由 `VREF=4.5 V`、PGA 和 ADS1299 24-bit 量程计算。

## OpenBCI 风格阻抗检测

串口控制栏的“阻抗检测”可对 CH1～CH8 分别显示电极阻抗和接触质量。固件使用
ADS1299 原生 `6 nA @ 31.25 Hz` 交流导联脱落激励；SRB1 接线自动写
`LOFF_SENSP`，SRB2/OpenBCI 接线自动写 `LOFF_SENSN`。

```text
A9 MASK
```

`MASK` 的 bit0～bit7 对应 CH1～CH8；`A9 00` 会关闭全部 LOFF 激励。
GUI 会读取并校验 ADS1299 的 `LOFF`、`LOFF_SENSP`、`LOFF_SENSN` 和
`LOFF_FLIP`，确认硬件确实开启或关闭。阻抗采用 31.25 Hz 正弦拟合，并按
6 nA 激励换算。点击“应用参考”后，板载串联电阻补偿自动切换：

| 参考模式 | 固定路径 | 自动补偿 |
| --- | --- | --- |
| SRB1 | `AINxP 4.99 kΩ + AREF/SRB1 4.99 kΩ` | 9.98 kΩ |
| SRB2 | `INxN 2.2 kΩ + SRB2 2.2 kΩ` | 4.40 kΩ |

补偿值仍可按对应接口的外部短接实测值手动校准。判定阈值为：小于 10 kΩ良好，
10～50 kΩ 可用，大于 50 kΩ 接触不良。

阻抗载波不属于正常 EEG。进入检测前，GUI 会结束当前 BIN 记录；退出检测后
保持停止状态，需要点击“开始采集”才能开始新的干净记录。

### SRB1 短接校准

在板外接口短接 `AIN1P` 与 `AREF` 时，测试电流仍会经过信号端和参考端的
两个 4.99 kΩ 电阻，未补偿读数理论值为 9.98 kΩ。实测约 10.2 kΩ 属于
正常范围，可由电阻公差、ADS1299 6 nA 激励电流误差和测量噪声造成。

校准时先将补偿设为 0 kΩ并记录稳定的短接读数，再将该读数填为补偿值；
本板可使用原理图标称值 9.98 kΩ，也可使用短接实测值约 10.2 kΩ。补偿不是
用来人为把状态调成“良好”，而是只扣除 PCB 固定串联阻抗。建议再串联
10 kΩ和 47 kΩ已知电阻验证，补偿后读数应分别接近对应阻值。

### SRB2 短接校准

SRB2/OpenBCI 模式使用 `LOFF_SENSN`，信号电极接 `INxN`，公共参考接
`SRB2`。在外部接口短接 `N1` 与 `SRB2` 时，电流经过两只 2.2 kΩ电阻，
未补偿理论读数为 4.40 kΩ。不要用 `N1–P1` 短接代替 `N1–SRB2` 回路校准。

## BIAS_SENSP

通道掩码通过以下三字节命令发送：

```text
A6 0D XX
```

`XX` 的 bit0–bit7 分别对应 CH1–CH8。GUI 和固件都会先用当前启用通道掩码过滤 `XX`。OpenBCI 默认 `BIAS P+N` 模式会把 mask 同时写入 `BIAS_SENSP` 和 `BIAS_SENSN`；“仅信号侧”模式在 SRB1 下只写 `BIAS_SENSP`，在 SRB2 下只写 `BIAS_SENSN`。

## 逐通道硬件设置

GUI 使用固定四字节命令更新一个通道：

```text
A7 CH GAIN FLAGS
```

- `CH`：0–7，对应 CH1–CH8。
- `GAIN`：1/2/4/6/8/12/24。
- `FLAGS bit0`：通道启用。
- `FLAGS bit1`：该通道加入当前参考模式对应的 BIAS 求和网络。
- `FLAGS bit2`：SRB2 模式下，该通道接入 SRB2。
- `FLAGS bit3–bit7`：保留。

参考模式使用以下两字节命令切换：

```text
A8 MODE
MODE=0：SRB1 全局参考
MODE=1：SRB2 逐通道参考
```

固件保证 SRB1 与 SRB2 互斥；内部短路和测试信号模式同时关闭两者。
ADS1299 原始极性在 SRB1 模式为 `INxP-SRB1`，SRB2 模式为
`SRB2-INxN`；GUI 不会静默翻转原始数据。

### 配置写入确认

GUI 修改 BIAS、PGA、通道电源或 SRB2 时，不再仅凭复选框推定成功：

1. 实时采集中先发送 `s` 停流；
2. 发送 `A6` 或 `A7`；
3. 固件重新配置并直接读回 ADS1299 的 `CHnSET`、`BIAS_SENSP`、
   `BIAS_SENSN` 和 `MISC1`；
4. 固件返回带异或校验的 12-byte 二进制 ACK；
5. GUI 只在 ACK 完整且固件寄存器校验通过时更新通道状态，随后发送
   `b` 恢复采集。

状态栏会显示实际读回的 P/N mask。没有 ACK、ACK 校验错误或 ADS1299
寄存器不匹配都会弹出“写入/校验失败”，不会再显示成已成功。

## OpenBCI 前端参考与 BIAS 接法

```text
测量电极 CH1～CH8 -> IN1N～IN8N（OpenBCI N1P～N8P 排针）
公共参考电极      -> SRB2
BIAS 电极         -> BIASOUT 板级输出
SRB1              -> 关闭
```

这是 OpenBCI 官方 EEG 接法：有效通道 `CHnSET.SRB2=1`，`MISC1.SRB1=0`，原始极性为 `SRB2-INxN`。OpenBCI 默认 Bias Include 会把有效通道同时加入 P/N BIAS；GUI 仍保留“仅信号侧”模式用于对比。禁用、开路、饱和或接触不良的通道不应加入 BIAS 求和网络。

## 实时显示性能

- 实时滤波仅在样本进入缓冲区时执行一次，绘图不再重复运行 `sosfiltfilt`。
- 原始和滤波数据使用批量环形缓冲写入。
- 串口以 2 ms 周期优先排空；CRC-CCITT 使用 C 实现，发生积压时暂缓重绘并快速追到最新帧。
- 波形以 12.5 FPS 刷新，8 个通道共享 X 轴并启用可视区裁剪和自动降采样；隐藏 Tab 不再重复绘制。
- 实时横轴使用本次采集累计样本数，不会在 90 秒环形缓冲写满后停止前进。
- PSD/Alpha 分析在后台线程中每秒更新一次，避免抢占串口与绘图线程。
- “诊断与 Alpha”页会显示 `Serial pending` 和 `Display lag est.`，正常稳定采集时后者应接近 0 s。

## 单通道放大

- 在八通道波形区双击任意 CH 波形，会自动切换到“单通道放大”Tab。
- 放大页只绘制所选通道，不与其他通道叠加，并显示通道电源、PGA、
  BIAS 和 SRB 状态。
- 放大页与主波形共用当前时间窗、原始/滤波状态和该通道纵轴范围。
- 放大曲线直接复用主绘图已经准备的数据，不会再次运行滤波。
- 单击波形仍只选择/高亮通道；放大页也可用下拉框切换 CH1～CH8。

## 目录

```text
ads1299_eeg_gui_native.py   主程序
requirements.txt           Python 依赖
install_and_run.bat         安装依赖并启动
run.bat                     直接启动
firmware/                   ESP32-C3 + ADS1299 固件
  ESP32C3_ADS1299_CH1_5_DEFAULT_PONLY_BIAS_GUI_PATCHED/
                            GUI 配套双参考固件，支持 A8 切换 SRB1/SRB2
  ESP32C3_ADS1299_OPENBCI_SRB2_REFERENCE/
                            OpenBCI 接线专用入口；默认 SRB2、BIAS P+N
  ESP32C3_ADS1299_SRB1_PONLY_REFERENCE/
                            固定 SRB1 旧硬件专用；不要用于 OpenBCI 接线
assets/                     全域智能 Logo 与应用图标
recordings/                 测试 BIN、CSV 和分析结果
  nme/                      导入 BIN 后自动生成的 MNE 交换 CSV（单位 V）
  fif/                      导入 BIN 后自动生成的双精度 *_raw.fif
TEST_REPORT_P0P1.txt        测试记录
CHANGELOG_P0P1.md           变更记录
```

## 注意事项

- GUI 中的 PGA 必须与 ADS1299 固件实际 PGA 一致，否则微伏数值会按错误比例换算。
- 自动MNE/FIF导出使用未滤波原始微伏数据；CRC坏帧会作为`BAD_frame`注释写入FIF。
- SHORTED/TEST 模式主要用于原始链路诊断，不应作为人体 EEG 或 Alpha 结果解释。
- “滤波后”画面更干净不代表硬件链路无工频、丢帧或饱和问题，应同时查看原始诊断数据。
- 本项目是采集与研究工具，不伪造医学事件，也不替代医疗诊断设备。
