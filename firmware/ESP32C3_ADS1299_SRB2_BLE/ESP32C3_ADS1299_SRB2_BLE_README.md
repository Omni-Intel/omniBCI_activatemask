# ESP32C3_ADS1299_SRB2_BLE

这是 `ESP32C3_ADS1299_SRB2_REFERENCE.ino` 的可靠 BLE 适配版。

## 命名

- Arduino 工程文件夹：`ESP32C3_ADS1299_SRB2_BLE`
- INO：`ESP32C3_ADS1299_SRB2_BLE.ino`
- BLE 设备名：`OmniBCI-C3-SRB2`

文件名和广播名均不带 `V3`。可靠传输的状态协议字节仍为 `0x03`，用于兼容现有 V8 的 ACK/NACK 解析，不属于产品名称。

## 保留的 SRB2 功能

- 默认参考：SRB2；测量电极接 INxN，公共参考接 SRB2。
- 默认 CH1–CH5 开启，CH6–CH8 关闭，250 SPS，PGA=24。
- 默认前端模式仍按参考固件：BIAS P+N。
- A8 仍支持 `0=SRB1`、`1=SRB2` 切换。
- A7 bit2 仍控制每通道 SRB2 开关。
- A6、A7、A8、A9、内部短路、内部方波、PGA、lead-off 均保留。
- 原始 ADS 48-byte 帧和 CRC 完全保留，MCU 不做滤波、不翻转极性。

## BLE 可靠层

- 4 个 48-byte EEG 帧组成一个 block。
- 每个 block 为 214 bytes，可在 MTU=247 时一次 Notify 发完。
- block 含 session ID、block sequence、first sample sequence、长度和 CRC。
- C3 保留 256 个尚未确认的 block，约 4.1 秒。
- GUI 累计 ACK 后才释放缓存；缺块可 NACK 重传。
- 32-block 滑动窗口，300 ms 超时重传，9 ms 发送节拍。
- BLE 活跃时关闭 USB EEG 镜像；BLE 未使用时保留 USB 原始流。

## 与现有 GUI 的关系

BLE Service/Characteristic UUID 和可靠 block 协议与 `ads1299_native_py_gui_P0P1_V8` 相同。

但当前 V8 GUI 的 **BLE 配置界面固定为 SRB1**，连接后会按 SRB1 发送 A7 配置，因此不能直接用它正确同步这份 SRB2 固件。固件本身已完成 BLE 适配；要在 GUI 中正确显示和配置 SRB2，需要把 GUI 的 BLE 模式改为根据设备名选择 SRB1/SRB2，并在 SRB2 下发送 A8=1 及 A7 bit2。

## Arduino IDE

- Board：ESP32C3 Dev Module（或实际 C3 板型）
- USB CDC On Boot：Enabled
- 推荐 Arduino-ESP32 Core：3.3.x
