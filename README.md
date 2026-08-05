# ads1299_native_py_gui_P0P1_V8

运行 `install_and_run.bat`，或安装 `requirements.txt` 后运行：

```powershell
py -3 ads1299_eeg_gui_native.py
```

窗口标题应包含：

```text
BLE Reliable V8 | SRB1/SRB2
```

## BLE 固件支持

本 GUI 同时支持：

- `OmniBCI-C3-SRB1-V3`：固定 SRB1 可靠 BLE 固件；
- `OmniBCI-C3-SRB2`：可切换 SRB1/SRB2 的可靠 BLE 固件；
- `OmniBCI-C3-ADS1299`：可选的统一设备名别名。

连接后不是只看蓝牙名称判断模式。GUI 会发送一次 A8/A7 探测，并读取固件返回的 ADS 配置 ACK：

- ACK reference=0：按固定 SRB1 配置，锁定参考选择框；
- ACK reference=1：确认支持 SRB2，开放 SRB1/SRB2 切换；
- 后续每个通道的 A7 和逻辑 BIAS mask 都会读回校验。

因此两个固件即使以后改成同一个广播名 `OmniBCI-C3-ADS1299`，GUI 仍能通过硬件读回区分。默认仍保留两个不同名称，避免两块板同时上电时只能靠蓝牙地址辨认。

## 接线提醒

### SRB1

- 测量电极：INxP
- 公共参考：SRB1
- BIAS：自动路由到 BIAS_SENSP
- 原始极性：INxP - SRB1

### SRB2

- 测量电极：INxN
- 公共参考：SRB2
- BIAS：自动路由到 BIAS_SENSN
- 原始极性：SRB2 - INxN

## 可靠 BLE 层

SRB1 与 SRB2 固件使用相同的可靠传输协议：4 个 48-byte EEG 帧组成一个 214-byte block，包含 session ID、block sequence 和 CRC；GUI 使用累计 ACK、NACK、乱序缓存和重传恢复缺块。
