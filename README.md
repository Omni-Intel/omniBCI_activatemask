# ADS1299 Native Python GUI - V18 BLE Capture/TX Isolation

V18 addresses a long-run BLE failure mode where the GUI can look idle while the firmware reports real `frameQueue` drops after a multi-second BLE notify stall.

## What changed

The critical firmware path is now split:

`ADS DRDY/read -> frameQueue -> reliable retention ring` runs independently from `BLE notify`.

BLE DATA notification has its own lower-priority task. A temporary Windows/BLE stack pause therefore fills the reliable retention ring instead of blocking the frameQueue consumer. The reliable ring stores roughly 9.2 seconds at 250 SPS.

The GUI also corrects BLE gap attribution using the full 32-bit firmware queue-drop counter, because the per-frame hint is only 8 bits and can wrap during a large drop burst.

## Run

Use `run.bat`.

## Firmware

SRB1: `firmware/ESP32C3_ADS1299_SRB1_BLE/ESP32C3_ADS1299_SRB1_BLE.ino`

SRB2: `firmware/ESP32C3_ADS1299_SRB2_BLE/ESP32C3_ADS1299_SRB2_BLE.ino`

See `CHANGELOG_V18.md` and `TEST_REPORT_V18.txt`.
