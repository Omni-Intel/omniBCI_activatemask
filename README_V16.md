# OmniBCI V16

## First run on a new Windows PC
Double-click `install_and_run.bat`. It creates `.venv`, installs only the packages needed for live acquisition, verifies them, then starts the GUI.

Later, use `run.bat`. Do not launch the `.py` by Windows file association if you want a reproducible environment.

BDF/MNE export is optional. Install it later with `install_optional_exports.bat`; failure of pyEDFlib does not block USB/BLE acquisition.

## Firmware
For SRB1 use:
`firmware/ESP32C3_ADS1299_SRB1_BLE/ESP32C3_ADS1299_SRB1_BLE.ino`

For SRB2 use:
`firmware/ESP32C3_ADS1299_SRB2_BLE/ESP32C3_ADS1299_SRB2_BLE.ino`

V16 GUI can read older V13/V14 compact reliable BLE frames, but the cross-PC BLE retransmission fix requires the V16 firmware.

## Diagnostics
Watch these fields when comparing computers:
- `Serial worker`: current/peak host RAM RX queue.
- `Serial gap/err`: longest reader-thread delivery gap and read errors.
- `Serial OS buffer`: whether the requested 1 MB driver buffer was accepted.
- `BLE adapt`: learned adapter class and DATA notify p95 gap.
- `BLE repair`: current adaptive NACK and stall-reconnect timing.
- `FW retained/flight`: retained reliable blocks and in-flight pressure.

A growing `FW retained` with low host queues points to the radio/Windows BLE path; a growing `Serial worker` points to host processing lag rather than a USB-driver loss.
