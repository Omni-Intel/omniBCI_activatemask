# ESP32C3_ADS1299_SRB1_BLE_V19

V19 is the only supported BLE firmware in this branch. It is fixed to SRB1 and
uses device-control protocol V1.

For direct COM control, build with `USB CDC On Boot = Enabled`. BLE-only builds
may leave it disabled; COM upload can still work in that configuration, but the
running sketch will not expose the same USB serial control channel.

- ADS acquisition task: priority 5
- frameQueue -> reliable retention pack task: priority 3
- BLE DATA notify task: priority 1
- DATA submissions use the Bluedroid `onStatus` result: congested notifications
  stay unsent in the retention ring and retry with bounded backoff.
- ADS software-SPI no longer masks interrupts for the complete ~2.9 ms frame,
  preventing BLE controller starvation every 4 ms.
- Capture queue: 512 frames (~2 s) of transient elasticity.
- Reliable retention: 384 six-frame blocks (~9.2 s at 250 SPS)
- Firmware version: V19.1.0
- Device-control protocol: V1
- GATT: DATA, CONTROL, STATUS, RESPONSE
- RESPONSE carries transaction-correlated HELLO, GET_CONFIG and SET_CONFIG replies.
- SET_CONFIG writes and reads back the complete relevant ADS1299 register set.
- SET_SAMPLE_RATE selects 250/500/1000 SPS and returns CONFIG1 plus the applied
  rate in the extended register snapshot.
- USB CDC serial accepts `AA <sample-rate-code>` and returns the same 12-byte
  CONFIG1/readback acknowledgement used by the STM32-compatible GUI.
- STATUS V5 is 96 bytes and carries heartbeat, reliable-transfer diagnostics,
  `config_generation`, missed-DRDY, late-read, mutex-busy, bad-status and
  maximum-read-time counters. The current hardware has no battery measurement
  source, so no battery percentage or voltage is reported.
- Runtime reference switching is not supported. Use the sibling fixed-SRB2
  myEMGpcb sketch when the PCB is wired for SRB2.
