# ESP32C3_ADS1299_SRB1_BLE_V19

V19 is the only supported BLE firmware in this branch. It is fixed to SRB1 and
uses device-control protocol V1.

- ADS acquisition task: priority 5
- frameQueue -> reliable retention pack task: priority 3
- BLE DATA notify task: priority 1
- DATA Notify acceptance is checked through the BLE callback; rejected sends
  remain pending and retry with bounded backoff.
- ADS software-SPI keeps interrupts enabled while reading a frame so BLE work
  is not starved during capture.
- Capture queue: 512 frames (~2 seconds at 250 SPS)
- Reliable retention: 384 six-frame blocks (~9.2 s at 250 SPS)
- Firmware version: V19.1.0
- Device-control protocol: V1
- GATT: DATA, CONTROL, STATUS, RESPONSE
- RESPONSE carries transaction-correlated HELLO, GET_CONFIG and SET_CONFIG replies.
- SET_CONFIG writes and reads back the complete relevant ADS1299 register set.
- STATUS V5 uses 96 bytes and carries heartbeat, acquisition,
  reliable-transfer and DRDY/read-time diagnostics plus `config_generation`.
  The current hardware has no battery measurement source, so no battery
  percentage or voltage is reported.
- SRB2 and runtime reference switching are not supported.
