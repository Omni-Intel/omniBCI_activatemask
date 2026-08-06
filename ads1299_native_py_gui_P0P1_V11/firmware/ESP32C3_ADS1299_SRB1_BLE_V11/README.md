# ESP32C3 ADS1299 SRB1 BLE V11

- Reference: fixed SRB1; signal electrodes on INxP.
- BLE name: `OmniBCI-C3-SRB1-V11`.
- BLE data layer: unchanged V8 protocol V3 reliable blocks, 4 frames/block, ACK/NACK and retransmission.
- USB path: when no BLE DATA subscription/session is active, frames go directly from the firmware queue to `Serial.write`, matching the proven P0P1 serial architecture.
- BLE and USB are not mirrored simultaneously.
