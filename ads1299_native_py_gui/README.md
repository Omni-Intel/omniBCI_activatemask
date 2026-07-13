# ADS1299 Native Python EEG GUI

这是重新按 Python 思路写的版本，不是把 MATLAB `uiaxes + filtfilt + pwelch` 逐行翻译过来。

## 为什么会更流畅

- GUI 使用 `PySide6 + pyqtgraph`，不是 `matplotlib` 实时重画。
- 实时显示用快速 IIR 滤波，不做每帧 `filtfilt` 全窗口零相位重算。
- PSD / Alpha / 50 Hz 指标约 1 秒刷新一次，波形约 80 ms 刷新一次。
- 串口解析和 raw bin 保存保持轻量，48-byte 帧格式不变。

## 安装运行

Windows 双击：

```bat
install_and_run.bat
```

已经装好依赖后可双击：

```bat
run.bat
```

手动运行：

```bat
py -3 -m pip install -r requirements.txt
py -3 ads1299_eeg_gui_native.py
```

## BIAS_SENSP 勾选框

八个通道勾选框会生成一个 mask：

- CH1-CH5 = `0x1F`
- CH1/CH3/CH5 = `0x15`
- CH1-CH8 = `0xFF`

点击 **应用 BIAS_SENSP** 会发送 3 个字节：

```text
A6 0D XX
```

其中 `XX` 是 mask。这个命令只改 ADS1299 的 `BIAS_SENSP` register `0x0D`，不改 `BIAS_SENSN`。

固件必须支持这个命令，否则按钮发送了也不会生效。

## 支持的固件帧格式

- 921600 baud
- 48-byte binary frame
- `A5 5A` sync
- 8 channel signed 24-bit ADS1299 data
- CRC16-CCITT over byte 0..45

## 注意

如果你的固件仍然是 CH1-CH5 默认开启、CH6-CH8 power-down，那么 GUI 勾 CH6-CH8 只会把这些 bit 写进 `BIAS_SENSP`，不会自动打开 CH6-CH8 采样。
