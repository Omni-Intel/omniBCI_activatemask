# ads1299_native_py_gui_P0P1_V8

运行 `install_and_run.bat`，或安装依赖后运行：

```powershell
py -3 ads1299_eeg_gui_native.py
```

窗口标题应包含 `BLE Reliable V8`。

## 必须配套的新固件

Arduino IDE 打开：

```text
firmware\ESP32C3_ADS1299_SRB1_BLE_V3\ESP32C3_ADS1299_SRB1_BLE.ino
```

旧版固件发送的是无确认 Notify 字节流；V8 GUI 接收的是 V3 固件的可靠 block 协议，二者不能混用。

## 可靠 BLE 数据链路

每个 DATA block 包含 4 个原有 48-byte ADS 帧，并增加：

- stream session id；
- block sequence；
- first sample sequence；
- frame count / payload length；
- block CRC16。

GUI 在 BLE 后台线程中重组 block，只把按序恢复出的原始 48-byte 帧交给旧解析器和 BIN 保存逻辑。因此：

- 原始 ADS 帧格式不变；
- BIN 仍是连续的 48-byte 帧流；
- BLE 分片、乱序与重传不会污染原始文件；
- GUI 不限幅、不裁剪、不插值。

GUI 每累计收到若干连续 block 会发送 cumulative ACK；发现缺块会发送 NACK range。固件保留最近约 4.1 秒的 block，优先重传缺块，并限制未确认发送窗口，防止 Windows 暂停接收时继续把 Notify 塞进不可控队列。

每次开始采集会生成新的 stream session id。旧录制中延迟到达的 ACK/数据会被拒绝，避免跨录制误释放缓存。

## 新增诊断

诊断页增加：

- `Reliable RX blocks`：收到 / 按序交付的 block；
- `Reliable pending`：等待缺块补齐的乱序 block；
- `Reliable repair`：收到的重传 block / 无法恢复的 gap marker；
- `Reliable ACK/NACK`；
- `Reliable dup/OOO`：重复 / 乱序 block；
- `Reliable CRC/sync`；
- `FW retained blocks`；
- `FW retrans/recover`；
- `FW overflow/unknown`。

理想状态：

```text
Sequence lost          = 0
Reliable pending       最终回到 0
Reliable retrans RX    可大于 0
FW overflow            = 0
Reliable CRC bad       = 0
```

`retrans RX > 0` 说明重传机制确实修复过无线缺块，不等于最终丢帧。
