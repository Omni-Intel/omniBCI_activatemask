# ONMI BCI activeMask / ADS1299 Bias Control

ESP32-C3 + ADS1299 8 通道脑电采集固件和 Python 上位机。

这个项目的目标是让上位机维护 `activeMask`，像 OpenBCI 一样控制哪些通道启用、哪些通道加入 ADS1299 的 Bias/RLD 共模反馈计算，避免未贴电极或未使用通道污染右腿偏置环路。

## 当前状态

已实机验收：

- Arduino CLI 编译通过。
- ESP32-C3 / COM3 烧录通过。
- 上位机可连接 `COM3 @ 921600`。
- `M01` 单通道配置通过：`BIAS_SENSP=0x01`，CH1 正常，CH2-CH8 关闭。
- `MFF` 全通道恢复通过：`BIAS_SENSP=0xFF`，CH1-CH8 全部正常。
- 固件 48 字节二进制数据帧格式保持不变。

## 目录结构

```text
firmware/
  ESP32C3_ADS1299_active_mask/
    ESP32C3_ADS1299_active_mask.ino

pc_app/
  active_mask_gui.py
  run_gui_D_env.cmd
  self_check.py
  requirements.txt
  environment.yml

tools/
  install_esp32_core.ps1
  compile_firmware.ps1
  upload_firmware.ps1
  launch_arduino_ide_ascii_cache.ps1

test_sketches/
  c3_serial_blink/

docs/
  openbci_cyton_reference_notes.md
```

## activeMask 定义

`activeMask` 是 8 bit 掩码：

| bit | 通道 | 接口 |
| --- | --- | --- |
| bit0 | CH1 | H1-9 |
| bit1 | CH2 | H1-8 |
| bit2 | CH3 | H1-7 |
| bit3 | CH4 | H1-6 |
| bit4 | CH5 | H1-5 |
| bit5 | CH6 | H1-4 |
| bit6 | CH7 | H1-3 |
| bit7 | CH8 | H1-2 |

规则：

- bit = 1：通道启用，`CHnSET=0x60`，加入 `BIAS_SENSP`。
- bit = 0：通道关闭，`CHnSET=0xE1`，不加入 `BIAS_SENSP`。
- 当前硬件使用 SRB1 共参考，Bias 采用 P-only：`BIAS_SENSP=activeMask`，`BIAS_SENSN=0x00`，`MISC1=0x20`。

## 串口命令

波特率：`921600`

| 命令 | 作用 |
| --- | --- |
| `MHH\n` | 设置 activeMask，`HH` 为两位十六进制，例如 `M01`、`MFF` |
| `1..8` | 关闭 CH1..CH8 |
| `! @ # $ % ^ & *` | 打开 CH1..CH8 |
| `b` | 开始 48 字节二进制数据流 |
| `s` | 停止数据流 |
| `?` | 停止数据流并打印诊断信息 |
| `q` | 内部短路测试 |
| `t` | 内部测试信号 |
| `o` | Bias off |
| `e` / `p` | 正常 activeMask + P-only Bias 模式 |

上位机发送 activeMask 时会先停流，再发 `MHH\n`，收到 `#ACK activeMask=0xHH` 后按需恢复采集，避免文本 ACK 混入二进制数据流。

## 固件编译和烧录

Arduino CLI 路径按当前 Windows 机器配置：

```powershell
D:\arduino_IDE\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe
```

先安装 ESP32 Arduino core：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_esp32_core.ps1
```

编译：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\compile_firmware.ps1
```

烧录到 COM3：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1 COM3
```

如果使用 Arduino IDE GUI，不要直接双击原始 Arduino IDE 图标。请使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\launch_arduino_ide_ascii_cache.ps1
```

原因是当前 Windows 用户路径包含中文和括号，ESP32 RISC-V linker 在 IDE 默认临时目录下可能报 `ld.exe cannot open output file`。项目脚本会强制使用 D 盘 ASCII 构建路径。

## 上位机启动

推荐在当前 Windows 机器上直接运行：

```powershell
cd /d D:\高博_采集板优化\active_mask_bias_control\pc_app
.\run_gui_D_env.cmd
```

该脚本直接调用：

```text
D:\conda_envs\gaobo_bci_active_mask\python.exe
```

这样可以绕开 `conda activate` 在中文 Windows 用户路径下触发的 OpenSSL activation script 路径问题。

通用环境创建方式：

```powershell
conda create -p D:\conda_envs\gaobo_bci_active_mask --override-channels -c conda-forge python=3.11 pip -y
D:\conda_envs\gaobo_bci_active_mask\python.exe -m pip install -r .\pc_app\requirements.txt
```

自检：

```powershell
D:\conda_envs\gaobo_bci_active_mask\python.exe .\pc_app\self_check.py
```

期望输出：

```text
self_check ok
```

## 上位机验收流程

1. 连接 `COM3 @ 921600`。
2. 点击 `Query ?`，确认当前寄存器。
3. 只勾选 CH1，点击 `Apply Mask`，再点击 `Query ?`。

期望：

```text
activeMask=0x01
BIAS_SENSP=0x01
BIAS_SENSN=0x00
CH1SET=0x60
CH2SET=0xE1 CH3SET=0xE1 CH4SET=0xE1
CH5SET=0xE1 CH6SET=0xE1 CH7SET=0xE1 CH8SET=0xE1
```

4. 点击 `All On`，`Apply Mask`，再 `Query ?`。

期望：

```text
activeMask=0xFF
BIAS_SENSP=0xFF
BIAS_SENSN=0x00
CH1SET=0x60 ... CH8SET=0x60
```

5. 点击 `Record Bin` 选择保存路径，点击 `Start Stream` 采集 10-30 秒。
6. 状态栏应接近 `rate=250Hz`，`crc=0`，`gaps=0/0`。
7. 点击 `Stop Stream`，再次点击 `Record Bin` 关闭文件。

保存时会生成：

```text
xxx.bin
xxx.json
```

JSON sidecar 会记录 activeMask、通道开关历史、串口参数、固件版本和采集时间。

## 数据帧

固件保持原 48 字节二进制帧格式：

- `[0..1]`：同步头 `0xA5 0x5A`
- `[2]`：协议版本
- `[3]`：帧类型
- `[4..7]`：sample sequence
- `[8..11]`：`micros()`
- `[12..14]`：ADS1299 STATUS
- `[15]`：flags
- `[16..39]`：8 路 24-bit 原始数据，MSB-first
- `[40..45]`：采样诊断字段
- `[46..47]`：CRC16-CCITT-FALSE

`activeMask` 不写入 48 字节帧，避免破坏现有解析脚本；上位机用 sidecar JSON 记录 mask 历史。

## 注意事项

- 同一时间只能有一个程序占用 COM3。使用上位机时不要同时打开 XCOM 或 Arduino Serial Monitor。
- 未使用通道建议关闭，不要让悬空输入参与 `BIAS_SENSP`。
- 当前版本不做实时波形显示；离线画图继续使用项目外的 bin-to-CSV/MNE 脚本。
- `docs/openbci_cyton_reference_notes.md` 是参考 OpenBCI 固件后的取舍说明。OpenBCI 的 SRB2/P-N Bias 默认配置不能直接套到本硬件，本项目以 `gaoboV2.net` 的 SRB1 + P-only Bias 为准。
