# ADS1299 SRB1 专用固件

本目录保留为名称明确、行为固定的 SRB1 专用版本。项目中的 GUI 配套固件
`ESP32C3_ADS1299_CH1_5_DEFAULT_PONLY_BIAS_GUI_PATCHED`
支持 SRB1/SRB2 双参考切换；本目录版本不会接受 SRB2 配置。

## 电极接法

```text
测量电极 CH1～CH8 -> IN1P～IN8P
公共参考电极      -> SRB1
BIAS 电极         -> BIASOUT
IN1N～IN8N        -> 不作为外部公共参考使用
```

SRB1 打开后，ADS1299 在内部把所有启用通道的反相输入接到 SRB1。
正常 EEG 模式的测量极性为 `INxP - SRB1`。

不要把 SRB1、SRB2、BIASOUT 或模拟地短接在一起。

## 固件保证

- 正常 EEG 模式：`MISC1(0x15) = 0x20`，SRB1 全局开启。
- 短路噪声和内部测试模式：`MISC1(0x15) = 0x00`。
- 所有通道的 `CHnSET.SRB2` 位始终为 0。
- 即使 GUI 的 `A7` 命令设置 bit2，本固件也会忽略该位。
- 默认模式为 CH1～CH5 开启、PGA 24、`BIAS_SENSP=0x1F`、
  `BIAS_SENSN=0x00`。
- GUI 和固件都会用当前启用通道掩码过滤 `BIAS_SENSP`，禁用通道
  不会进入 BIAS 求和网络。
- `A7` 命令仍支持逐通道启用、PGA 和 BIAS_P：

```text
A7 CH GAIN FLAGS
FLAGS bit0 = 通道启用
FLAGS bit1 = 加入 BIAS_SENSP
FLAGS bit2 = 忽略，SRB2 永远关闭
```

## 烧录

Arduino IDE 板型选择 ESP32-C3，并启用 `USB CDC On Boot`。打开：

```text
ESP32C3_ADS1299_SRB1_PONLY_REFERENCE.ino
```

串口波特率为 `921600`。

连接人体电极时必须采用电池供电和满足要求的电气隔离，不能让未隔离的
USB 或市电设备形成到人体的导电通路。
