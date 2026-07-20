# Omni-Intelligence ADS1299 EEG Viewer

这是一个面向 ADS1299 + ESP32-C3 的原生 Python 脑电采集、BIN 录制和离线回放工具。界面采用紧凑的临床脑电走纸布局，保留真实微伏标定、原始数据诊断、滤波显示副本和 MNE 联动能力。

## 主要功能

- 解析固件输出的 48-byte 二进制帧并校验 CRC16-CCITT。
- 通过 921600 baud 串口实时采集 8 个 ADS1299 通道。
- 保存原始串口 BIN；显示处理不会改写原始数据。
- 打开历史 BIN，使用全程导航条、拖动、滚轮和方向键回放。
- 将原始微伏数据导出为 CSV。
- 一键构造 MNE `RawArray` 并打开 MNE 浏览器。
- 支持 PGA 1/2/4/6/8/12/24，与固件实际增益同步标定。
- 支持 BIAS_SENSP 通道掩码控制。
- 提供原始 RMS、峰峰值、50 Hz 占比、丢帧、CRC 和 Alpha 质量诊断。

## GUI 布局

- 白色/浅灰临床监测界面，橙色用于标题、选中项和关键曲线。
- 主工具栏左侧显示 Omni-Intelligence 横版 Logo；标题栏和任务栏使用方形 OI 图标。
- 左侧固定 `CH1`–`CH8` 通道栏和通道状态标识。
- 中央黑色 EEG 走纸区，浅灰波形、秒级主网格和 0.2 秒次网格。
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
| 工频陷波 | 50 Hz |

滤波只作用于显示副本。关闭“滤波后”后显示逐通道去直流的原始微伏数据，BIN 文件始终保存原始串口字节。

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

## BIAS_SENSP

通道掩码通过以下三字节命令发送：

```text
A6 0D XX
```

`XX` 的 bit0–bit7 分别对应 CH1–CH8。该命令只修改 `BIAS_SENSP (0x0D)`，不修改 `BIAS_SENSN`。固件必须实现此命令后 GUI 控制才会生效。

## 目录

```text
ads1299_eeg_gui_native.py   主程序
requirements.txt           Python 依赖
install_and_run.bat         安装依赖并启动
run.bat                     直接启动
firmware/                   ESP32-C3 + ADS1299 固件
assets/                     Omni-Intelligence Logo 资源
  omni_logo_cnen.png        工具栏横版 Logo
  omni_logo_mark.png        窗口和任务栏图标
recordings/                 测试 BIN、CSV 和分析结果
TEST_REPORT_P0P1.txt        测试记录
CHANGELOG_P0P1.md           变更记录
```

## 注意事项

- GUI 中的 PGA 必须与 ADS1299 固件实际 PGA 一致，否则微伏数值会按错误比例换算。
- SHORTED/TEST 模式主要用于原始链路诊断，不应作为人体 EEG 或 Alpha 结果解释。
- “滤波后”画面更干净不代表硬件链路无工频、丢帧或饱和问题，应同时查看原始诊断数据。
- 本项目是采集与研究工具，不伪造医学事件，也不替代医疗诊断设备。
