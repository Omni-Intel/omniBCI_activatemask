# ESP32C3 ADS1299 SRB2 BLE V11

- Default reference: SRB2; signal electrodes on INxN and common reference on SRB2.
- Runtime A8/A7 SRB1/SRB2 controls are retained.
- BLE name: `OmniBCI-C3-SRB2-V11`.
- BLE data layer: unchanged V8 protocol V3 reliable blocks, 4 frames/block, ACK/NACK and retransmission.
- USB path: when no BLE DATA subscription/session is active, frames go directly from the firmware queue to `Serial.write`, matching the proven P0P1 serial architecture.
- BLE and USB are not mirrored simultaneously.
