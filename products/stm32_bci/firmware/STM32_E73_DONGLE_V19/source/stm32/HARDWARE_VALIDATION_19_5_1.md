# STM32 19.5.1 hardware validation

Date: 2026-09-07

Validated on the STM32H563 test board with the existing E73 and USB Dongle firmware.
Generated images, signing keys, RTT logs and recordings remain outside Git.

## Passed

- J-Link application programming and verification at 100 kHz SWD.
- Autonomous 250 SPS acquisition with all eight channels enabled, gain 24 and SRB1.
- Boot without a card while acquisition and the RF producer continue.
- FAT32 card insertion during autonomous acquisition without resetting the board.
- Automatic creation and recording of `BCI00009.BIN` after late insertion.
- Normal Dongle stop followed by stopped/file-closed/no-error acknowledgment.
- Readback of `BCI00008` and `BCI00009`: 18,700 total 48-byte frames, zero bad headers, CRC failures, sequence breaks and trailing bytes.
- Matching MET start/progress/end records, including exact final byte and sequence boundaries; zero reported SD frame/event drops.

## Still pending

- Locate the repeatable approximately 248.65 frames/s cadence relative to MCU uptime. File sequence continuity proves no dropped constructed frames, not that every physical ADS1299 DRDY edge was serviced.
- Physical external-trigger debounce and event association test.
- SD full/write-failure behavior and sudden power-loss testing.
- Absolute UTC synchronization and PC-side MET import.

This validation applies to the STM32 product only. It does not change either ESP32 product, the E73 firmware, the Dongle firmware or the 48-byte wireless frame.
