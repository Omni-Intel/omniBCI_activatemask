# ADS1299 Native Python EEG GUI — P0+P1 滤波升级版

这版保留原来的 **48-byte 帧、串口控制、BIAS_SENSP、bin 录制和离线导入**，重点重构了后端信号处理。固件无需为这些滤波改动重新修改。

## 本版关键变化

### 1. 实时波形改为连续有状态滤波

实时数据进入 GUI 后按样本连续执行：

```text
原始 uV → 1–40 Hz 二阶 Butterworth → 50 Hz Q=30 陷波 → filtered ring
```

滤波器状态会跨 GUI 刷新周期保留，不再每 80 ms 从零重滤整个显示窗口，因此不会因为窗口长度或刷新动作反复产生左端启动瞬态。

离线 bin 显示使用零相位滤波；保存的 bin 始终仍是原始串口字节。

### 2. 原始诊断与 Alpha 分析彻底分离

右侧现在同时显示：

- `Raw RMS`：未带通、未陷波，只去直流后的输入端 RMS
- `Filtered RMS`：独立 Alpha 链处理后的 RMS
- `Raw peak-to-peak`
- `50Hz/raw ratio`：只用原始 PSD 计算，陷波器无法把问题“藏起来”
- `Quality window`：当前 Alpha 窗是否通过质量检查

勾选 **PSD显示原始诊断** 时画原始 PSD；不勾选时画独立 Alpha 链 PSD。这个开关只改变绘图，不改变 Alpha 算法。

### 3. Alpha 链固定，不受“实时滤波显示”开关影响

Alpha 分析固定使用：

```text
原始数据 → 线性去趋势 → 50 Hz Q=30 陷波 → 1–40 Hz → 4 秒 Hann PSD
```

不会再因为关闭波形滤波而突然改用未滤波数据。

### 4. 75% 重叠 + 坏片段拒绝

最近 10 秒被切成多个 **4 秒片段、1 秒步进（75% overlap）**。每个片段先检查：

- 有效样本比例至少 99%
- 连续缺失不超过 2 点
- 无帧序号跳变
- 无模式切换
- 未接近满量程
- EEG 模式峰峰值不超过 250 uV
- 不是平线

坏片段不参与 Alpha PSD；合格片段的 PSD 使用中位数合并，避免一次眨眼或触碰直接污染全部频谱。SHORTED 和 TEST 模式只做原始诊断，不计算 Alpha。

### 5. OpenBCI 风格的频谱时间平滑

PSD 绘图在 dB 域做指数平滑：

```text
当前显示 = 0.85 × 上次显示 + 0.15 × 当前 PSD
```

这只稳定频谱显示，不修改原始数据，也不伪造时域波形。

### 6. 20 秒睁眼/闭眼统计

点击 **采集20秒睁眼** 或 **采集20秒闭眼** 后：

- 每秒收集一次合格 Alpha 结果
- 坏窗口自动跳过
- 至少需要 10 个合格结果
- 最终保存 20 秒内 Alpha power 的中位数
- 两种状态都完成后显示 `Closed/Open Alpha` 的 dB 差值

离线 bin 模式下，按钮直接统计当前位置之前最近 20 秒。

## 安装运行

第一次运行双击：

```bat
install_and_run.bat
```

已经安装依赖后双击：

```bat
run.bat
```

手动运行：

```bat
py -3 -m pip install -r requirements.txt
py -3 ads1299_eeg_gui_native.py
```

## BIAS_SENSP 勾选框

八个通道勾选框会生成 mask：

- CH1–CH5 = `0x1F`
- CH1/CH3/CH5 = `0x15`
- CH1–CH8 = `0xFF`

点击 **应用 BIAS_SENSP** 发送：

```text
A6 0D XX
```

其中 `XX` 是 mask。该命令只改 ADS1299 `BIAS_SENSP` 寄存器 `0x0D`，不改 `BIAS_SENSN`。

## 支持的固件帧格式

- 921600 baud
- 48-byte binary frame
- `A5 5A` sync
- 8 channel signed 24-bit ADS1299 data
- CRC16-CCITT over byte 0..45

## 使用提醒

- **ADS internal short** 时请勾选“PSD显示原始诊断”，重点看 `Raw RMS` 和原始 PSD；不要拿 `Filtered RMS` 判断芯片底噪。
- 250 uV 峰峰值门限针对人体 EEG Alpha 检测。信号源输入、ECG、EMG 或大幅测试波形可能会被正确标为坏窗，但原始波形和原始 PSD 仍然显示。
- GUI 的增益必须和固件中 ADS1299 的实际 PGA 一致，否则所有 uV 数值都会按错误倍数缩放。
