# BCI-Band STM32H563 production firmware

Data path:

`ADS1299 -> STM32H563 -> 48-byte frame -> SPI3 -> E73 -> ESB -> USB dongle`

USB is a composite device with two CDC ports:

- `BCI-Band Data CDC` carries the unchanged 48-byte data frames when the host
  asserts DTR.
- `BCI-Band DFU CDC` carries MCUboot/mcumgr firmware-update traffic. It is not
  a data serial port.

This makes the STM32 independently detectable, permits comparing the local USB
stream with the dongle stream, and allows later STM32 upgrades without J-Link.

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

## Flash layout

| Region | Address | Size |
| --- | --- | --- |
| MCUboot | `0x08000000` | 64 KiB |
| Active application (`slot0`) | `0x08010000` | 448 KiB |
| Downloaded update (`slot1`) | `0x08080000` | 448 KiB |
| MCUboot storage | `0x080F0000` | 64 KiB |

The application confirms a trial image only after ADS1299 initialization has
succeeded. If the new image cannot initialize the acquisition path, MCUboot can
roll back on the following reset.

## Build

Set `$halStm32` to a compatible Zephyr `hal_stm32` module checkout.
The build uses an ECDSA-P256 signing key at
`keys/stm32-mcuboot-ecdsa-p256.pem`. The key is intentionally ignored by Git;
back it up securely because every future update must be signed by this key.

```powershell
$project = (Resolve-Path .).Path
$halStm32 = (Resolve-Path $env:HAL_STM32_PATH).Path

nrfutil toolchain-manager launch --ncs-version v3.4.0 `
  -- west build -p always --sysbuild `
  -b bciband_h563vg `
  -d build_dfu $project `
  -- `
  -DBOARD_ROOT=$project `
  -DZEPHYR_EXTRA_MODULES=$halStm32
```

## First installation with J-Link

The first DFU-capable installation must replace the old single-image layout.
Connect J-Link to the STM32 SWD header and program the combined factory image:

```text
device STM32H563VG
if SWD
speed 4000
connect
r
h
loadfile release_dfu/bciband_stm32_v19_1_usb_dfu_factory.hex
r
g
exit
```

The same commands are provided in `jlink_flash_stm32_dfu_factory.jlink`.
After reset, Windows should enumerate both Data CDC and DFU CDC ports.

## Later updates through USB

Install a compatible `mcumgr` command-line client, connect the STM32 USB port,
and use the COM number belonging to `BCI-Band DFU CDC`:

```powershell
$dfuConnection = 'dev=COM_NUMBER,baud=115200,mtu=512'
mcumgr --conntype serial --connstring $dfuConnection image upload `
  release_dfu\bciband_stm32_v19_1_usb_dfu_update.bin
mcumgr --conntype serial --connstring $dfuConnection image list
mcumgr --conntype serial --connstring $dfuConnection image test IMAGE_HASH_FROM_LIST
mcumgr --conntype serial --connstring $dfuConnection reset
```

Do not upload the factory HEX over mcumgr. The USB update input is the signed
`update.bin`; the combined factory HEX remains the J-Link recovery image.

## Release SHA-256

```text
6304F7EF717301F0CD83B0AF66BFF7458198729A94A484D11DD50B5666E0F7C7  bciband_stm32_v19_1_usb_dfu_factory.hex
2F053E12351132749408D6A6BC8E2A2D0BE6577C444CB462C23750264F8CF4A5  bciband_stm32_v19_1_usb_dfu_update.bin
76F53AB3C75B6E6671BCBAF82F72A1F275608DB2CD1D09C5F75B4C8D7F4BBAA4  bciband_stm32_v19_1_usb_dfu_update.zip
```
