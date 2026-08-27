# OmniBCI V19 STM32 compatibility firmware

This package implements the V19 data/control path for:

```text
ADS1299 -> STM32H563VGT6 -> E73-2G4M08S1C ~~ 2.4 GHz ~~ nRF52840 dongle -> USB CDC -> OmniBCI V19
```

The dongle exposes a virtual COM port. Its 48-byte sample frames and bidirectional control messages are compatible with the OmniBCI V19 GUI.

## Implemented controls

- SRB1-only acquisition; CH1-CH8 enabled by default
- 250, 500, and 1000 SPS with ADS1299 register readback
- Per-channel enable, PGA gain, and BIAS inclusion
- Internal short, internal test, and electrode-impedance modes
- PE5: 200 kHz, 50% PWM for NSC1002
- PA1: active-high streaming/work indicator
- Configuration forwarding from USB CDC through the dongle/E73 link to STM32

## Release images

For a new installation, flash these three images from `release/`:

1. `04_stm32h563_v19_1_usb_dfu_factory.hex` to STM32H563VGT6
2. `02_e73_v19_full_control.hex` to E73-2G4M08S1C (nRF52840)
3. `03_dongle_v19_full_control.hex` to the nRF52840 USB dongle

`01_stm32h563_v19_full_control.hex` is the legacy STM32 image without MCUboot.
Do not flash it after adopting USB DFU unless intentionally reverting to the
old flash layout.

For each target, connect J-Link over SWD and use the matching device name:

```text
device STM32H563VG       # STM32 target
device NRF52840_XXAA     # E73 or dongle target
si SWD
speed 1000
connect
r
h
loadfile <absolute-path-to-the-matching-hex>
r
g
q
```

After flashing, remove J-Link. Power the STM32 acquisition board from its battery and plug only the USB dongle into the PC. Select the dongle COM port in OmniBCI V19; the GUI automatically synchronizes its selected sample rate during connection, so a separate first-click on “Apply sample rate” is not required.

The STM32 USB connector now enumerates two virtual COM ports. **BCI-Band Data
CDC** is the local 48-byte sample stream; **BCI-Band DFU CDC** is reserved for
signed MCUboot/mcumgr updates. Later STM32 updates use
`05_stm32h563_v19_1_usb_dfu_update.bin`; the ZIP is also supplied as artifact
`06_stm32h563_v19_1_usb_dfu_update.zip`. See `source/stm32/README.md` for the
exact upload and rollback procedure.

## SHA-256

- STM32: `A9D3DC17182DE5764F9A7BCDF60A1B9FE95A18F7285AC41BDD2E72BF6F6DC587`
- E73: `802E1639F20B35AC47A0E857CBF3093CDA49C3A23E1F0C94ABE51117F54B6298`
- Dongle: `BD5618745BA9B5D71818A44085C53B9BEBC726EBA66AA3E58B2361C3955977C4`
- STM32 USB-DFU factory HEX: `6304F7EF717301F0CD83B0AF66BFF7458198729A94A484D11DD50B5666E0F7C7`
- STM32 USB-DFU update BIN: `2F053E12351132749408D6A6BC8E2A2D0BE6577C444CB462C23750264F8CF4A5`
- STM32 USB-DFU update ZIP: `76F53AB3C75B6E6671BCBAF82F72A1F275608DB2CD1D09C5F75B4C8D7F4BBAA4`

Zephyr source projects and J-Link scripts are under `source/stm32`, `source/e73`, and `source/dongle`. Generated build directories are intentionally excluded.

> Safety: when electrodes are connected to a person, use battery power and appropriate medical-grade electrical isolation. Do not create a conductive path to non-isolated USB or mains-powered equipment.
