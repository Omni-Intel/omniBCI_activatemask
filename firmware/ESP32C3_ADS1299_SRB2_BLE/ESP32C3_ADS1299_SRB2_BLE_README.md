# ESP32C3_ADS1299_SRB2_BLE_V16

- V16 compact reliable BLE protocol V2.
- Six EEG samples per reliable block; packet size 238 bytes at a full block.
- 320-block RAM retention: approximately 7.68 seconds at 250 SPS.
- 16-block in-flight window with cumulative ACK/NACK retransmission.
- 256-frame ADS acquisition queue.
- USB still emits the original 48-byte frames when BLE has not started a DATA session.
- Internal Flash is deliberately not used as a per-sample FIFO because erase/program stalls and wear can hurt real-time acquisition.
- Requires the V16 GUI for compact-block reconstruction.
