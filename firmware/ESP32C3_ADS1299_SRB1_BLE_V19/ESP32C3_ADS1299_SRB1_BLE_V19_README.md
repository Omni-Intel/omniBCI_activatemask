# ESP32C3_ADS1299_SRB1_BLE_V19

V19 is the only supported BLE firmware in this branch. It is fixed to SRB1 and
uses device-control protocol V1.

- ADS acquisition task: priority 5
- frameQueue -> reliable retention pack task: priority 3
- BLE DATA notify task: priority 1
- Reliable retention: 384 six-frame blocks (~9.2 s at 250 SPS)
- Firmware version: V19.0.0
- Device-control protocol: V1
- GATT: DATA, CONTROL, STATUS, RESPONSE
- RESPONSE carries transaction-correlated HELLO, GET_CONFIG and SET_CONFIG replies.
- SET_CONFIG writes and reads back the complete relevant ADS1299 register set.
- STATUS carries heartbeat, acquisition and reliable-transfer diagnostics plus
  `config_generation`. The current hardware has no battery measurement source,
  so no battery percentage or voltage is reported.
- SRB2 and runtime reference switching are not supported.
