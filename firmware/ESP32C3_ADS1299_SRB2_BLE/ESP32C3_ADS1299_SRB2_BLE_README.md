# ESP32C3_ADS1299_SRB2_BLE_V18

V18 keeps the same compact reliable BLE wire protocol but isolates DATA notify from the capture/packing path.

- ADS acquisition task: priority 5
- frameQueue -> reliable retention pack task: priority 3
- BLE DATA notify task: priority 1
- Reliable retention: 384 six-frame blocks (~9.2 s at 250 SPS)
- Requires/strongly recommends the V18 GUI for corrected gap attribution and long-run diagnostics.
