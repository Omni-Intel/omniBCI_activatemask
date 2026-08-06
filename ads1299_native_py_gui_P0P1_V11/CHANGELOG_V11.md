# V11 changes

## GUI

- Restored the no-suffix P0P1 USB scheduler: 2 ms polling, up to 6 ms aggressive drain, receive-before-paint, and 80 ms plot refresh.
- Retained V8 BLE bounded receive batches, reliable block reordering/retransmission, ACK/NACK, and adaptive delayed display.
- Requests a 1 MB Windows serial receive buffer when pyserial/driver support it.
- USB no longer inserts NaN timeline columns in the receive hot path; BLE still preserves real-time gaps for display.
- Sequence gaps are classified as MCU-evidenced or host-side suspected using pending/backlog and queue-drop fields.
- Serial start follows the lightweight P0P1 start sequence; BLE retains reliable-session reset behavior.

## Firmware

- V8 reliable BLE implementation is retained.
- Added an automatic USB fast path when no BLE DATA subscription/session is active.
- Added V11 SRB1/SRB2 broadcast names.
- Included the original no-suffix P0P1 serial firmware as a control build.

## Packaging

- Top-level folder and firmware sketch folders contain no parentheses.
- Arduino sketch folder names match their `.ino` filenames.
