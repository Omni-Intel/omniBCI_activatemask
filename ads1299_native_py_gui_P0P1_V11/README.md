# ADS1299 Native Python GUI P0P1 V11

V11 is built from two proven baselines:

- USB/serial baseline: `ads1299_native_py_gui_P0P1` without a version suffix.
- BLE baseline: `ads1299_native_py_gui_P0P1_V8` reliable BLE transport.

The main change is not BLE cold start. V11 gives USB and BLE separate host-side schedulers.

## Why the old serial version was steadier

The no-suffix P0P1 GUI polls USB every 2 ms, drains all bytes already waiting for up to about 6 ms, and drains again before repainting. Its live plot refresh is about 80 ms.

V8 used one compromise scheduler for both links: 5 ms polling, small bounded batches, and 50 ms plotting. That protects the Qt event loop from Windows BLE bursts, but it gives USB less time to empty the driver buffer. When plotting or analysis briefly occupies the UI thread, USB bytes can accumulate and a later sequence gap may be reported.

V11 therefore uses:

- USB: 2 ms poll, up to 6 ms drain, 80 ms plot, receive-before-paint, 1 MB Windows RX buffer when supported.
- BLE: V8 bounded batches, reliable block reorder/retransmission, ACK/NACK, and adaptive delayed playback.

## Firmware choices

### Recommended hybrid firmware

Choose one according to the PCB reference connection:

- `firmware/ESP32C3_ADS1299_SRB1_BLE/ESP32C3_ADS1299_SRB1_BLE.ino`
- `firmware/ESP32C3_ADS1299_SRB2_BLE/ESP32C3_ADS1299_SRB2_BLE.ino`

These retain V8 reliable BLE. When no BLE DATA subscription/session exists, V11 takes a simple queue-to-USB fast path matching the proven serial firmware.

### Serial-only control firmware

- `firmware/ESP32C3_ADS1299_SERIAL_STABLE/ESP32C3_ADS1299_SERIAL_STABLE.ino`

This is the proven no-suffix P0P1 serial firmware logic, included unchanged apart from the banner/folder name. Use it to determine whether any remaining loss comes from the hybrid firmware/BLE stack or from the PC/GUI.

## Start

1. Extract the ZIP to a short ASCII path without parentheses, for example `C:\OmniBCI\V11`.
2. Do not use a folder such as `V11 (1)`; some ESP32 Arduino Windows build commands fail on parentheses and show a garbled `bootloader.bin` error.
3. Run `install_and_run.bat`, or install `requirements.txt` and run `ads1299_eeg_gui_native.py`.
4. Select `USB 串口` or `BLE 无线` in the GUI.

## Understanding the diagnostics

- `Sequence gaps`: confirmed missing sequence numbers.
- `Gap source est. MCU`: the same frame also reported DRDY pending/backlog or firmware queue-drop evidence.
- `Gap source est. host`: sequence jumped without those firmware hints; the likely loss point is Windows USB/BLE delivery, GUI scheduling, or parser resynchronisation.
- `CRC bad` / `Sync drop bytes`: direct evidence of damaged/misaligned host-side bytes.
- `Timeline gap fill`: only used for BLE display timing. USB does not allocate NaN gap columns on its hot receive path.

The source estimate is diagnostic, not mathematical proof. The firmware counters and sequence numbers should be considered together.

## Suggested test order

1. Burn `SERIAL_STABLE_V11`, use USB for 5–10 minutes, and record the diagnostics page.
2. Burn the matching SRB1/SRB2 BLE V11 firmware and repeat USB mode.
3. Use BLE mode and compare `Reliable repair`, `Notify gap`, `Display underrun`, and firmware overflow counters.

This three-step comparison separates GUI/PC receive issues from firmware/BLE-stack effects.
