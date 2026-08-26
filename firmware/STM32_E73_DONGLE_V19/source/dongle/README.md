# BCI-Band nRF52840 USB dongle

接收 E73 发出的 ESB 动态载荷并通过 USB CDC 原样输出。除协议版本 1
的 48 字节 EEG 帧外，当前版本还持续读取 CDC OUT，将 GUI 命令封装为
带序号/CRC 的 ESB ACK payload；E73 返回的控制 ACK 会还原为原始字节写回
CDC IN。

无线参数：ESB DPL、PRX、1 Mbps、频道 80、CRC16、自动应答，地址参数与 `e73_dongle` 完全一致。

构建：

```powershell
D:\nrfutil\nrfutil.exe toolchain-manager launch --ncs-version v3.4.0 --chdir C:\ncs\v3.4.0 -- west build -p always -b nrf52840dongle/nrf52840/bare -d C:/Users/Jermayn/Desktop/qwer/qwer/bciband/dongle/build C:/Users/Jermayn/Desktop/qwer/qwer/bciband/dongle
```

J-Link 直烧必须使用 `bare` 变体，使复位向量位于 `0x00000000`；不带
`bare` 的板型会保留原厂 USB bootloader/MBR 布局，单独直烧应用 HEX 后
可能无法启动。产物：`build/dongle/zephyr/zephyr.hex`。上位机打开 CDC
串口并置 DTR 后，每次读取固定 48 字节；不要按文本行读取。
