# E73 dongle-compatible firmware

This project is independent from `../e73`; it does not modify the existing BLE
firmware.

## Compatibility contract

- Nordic ESB DPL, PTX, 1 Mbps, RF channel 80, pipe 0
- ESB CRC16 and ACK enabled, one retry at 500 us, +8 dBm
- base0 `45 57 54 73`, base1 `12 99 24 08`
- prefixes `A5 C2 C3 C4 C5 C6 C7 C8`
- one radio payload is exactly one 48-byte EWT ADS1299 frame
- bidirectional control uses CRC-protected command packets in ESB ACK payloads;
  replies are ordinary PTX payloads and do not alter the 48-byte EEG format

Frame bytes:

- `0..1`: `A5 5A`
- `2`: protocol version `01`
- `3`: record type `01`
- `4..7`: sequence, little-endian
- `8..11`: timestamp, little-endian
- `12..14`: ADS1299 status bytes
- `15`: reserved
- `16..39`: eight signed 24-bit channels, big-endian
- `40..41`: sample-rate marker, little-endian
- `42..45`: reserved
- `46..47`: CRC16-CCITT-FALSE over bytes `0..45`, little-endian

## Current PCB SPI pins

- SCK P0.13 (E73 pad 33)
- MOSI P0.20 (E73 pad 32)
- MISO P0.17 (E73 pad 30)
- CSN P0.22 (E73 pad 34)
- SPI mode 0, MSB first, exactly 48 bytes per CS transaction; MOSI carries an
  EEG frame/control reply while MISO concurrently carries the next GUI command

## Artifacts

- `build_selftest/selftest/zephyr/zephyr.hex`: sends simulated valid records at
  1000 Hz;
  use this first to prove the E73-to-dongle radio and USB path.
- `build_bridge/e73_dongle/zephyr/zephyr.hex`: waits for real 48-byte records
  from STM32 SPI.

The current STM32 PWM firmware does not yet produce these records, so the
bridge image will be silent until ADS1299 acquisition and STM32 SPI framing are
implemented.
