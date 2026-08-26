# BCI-Band STM32H563 production firmware

Data path:

`ADS1299 -> STM32H563 -> 48-byte frame -> SPI3 -> E73 -> ESB -> USB dongle`

The same 48-byte frame is also written unchanged to the STM32 USB CDC port
when the host asserts DTR. This makes the STM32 independently detectable and
allows comparing the local USB stream with the dongle stream.

The stream metadata follows the OmniBCI V19 application contract: byte 15
contains the ADS status/DRDY/BIAS/SRB1 flags, bytes 40..41 contain the measured
ADS read time in microseconds, byte 42 is the consumed DRDY count, and byte 43
is mode 1 (EEG, BIAS P-only, SRB1). The acquisition queue fields at bytes
44..45 are zero because this firmware reads one frame directly per semaphore
wakeup instead of using an intermediate acquisition queue.

## Confirmed PCB pin map

| Function | MCU pin | LQFP100 pad |
| --- | --- | ---: |
| ADS DRDY | PA4 | 29 |
| ADS SCK | PA5 | 30 |
| ADS MISO | PA6 | 31 |
| ADS MOSI | PA7 | 32 |
| ADS CS | PB2 | 37 |
| ADS START | PE8 | 39 |
| ADS RESET | PE10 | 41 |
| USB D- / D+ | PA11 / PA12 | 70 / 71 |
| 工作指示灯 | PA1 | 24，高电平点亮 |
| E73 RESET / IRQ | PD5 / PD6 | 86 / 87 |
| E73 SCK / MISO / MOSI | PB3 / PB4 / PB5 (SPI3) | 89 / 90 / 91 |
| E73 CSN | PB9 | 96 |
| NSC1002 clock | PE5 (TIM15_CH1) | 4 |

ADS configuration boots at 250 SPS, gain 24, CH1-CH8 enabled, SRB1 on,
BIAS_SENSP `0xFF`,
BIAS_SENSN `0x00`. Runtime command `AA CODE` selects 250/500/1000 SPS for
`CODE=0/1/2`; the firmware stops acquisition at the rate boundary and returns
a 12-byte `BC` ACK containing the CONFIG1 readback and applied rate.

PA1 工作灯采用数据链健康逻辑：只有 ADS1299 成功读帧且 SPI3 成功把该帧
交给 E73 时点亮；停止采集或连续 100 ms 没有成功帧时熄灭。它不依赖
STM32 USB 是否连接。

## Build

Run this from `firmware/STM32_E73_DONGLE_V19/source/stm32` with nRF Connect SDK
v3.4.0. Set `halStm32Path` to the `hal_stm32` module in your local SDK/workspace.

```powershell
$stm32Project = (Resolve-Path .).Path
$halStm32Path = 'C:\path\to\hal_stm32'
nrfutil toolchain-manager launch --ncs-version v3.4.0 `
  -- west build -p always `
  -b bciband_h563vg `
  -d build `
  $stm32Project `
  -- `
  -DBOARD_ROOT=$stm32Project `
  -DZEPHYR_EXTRA_MODULES=$halStm32Path
```
