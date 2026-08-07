# V17 long-run continuity

This revision targets long BLE recordings where the waveform could pause, then burst forward, especially during BIAS drift or ADC saturation.

- Bleak DATA callback now only timestamps and enqueues bytes; reliable CRC/reassembly/compact expansion runs in a dedicated decoder thread.
- Reliable ACK cadence stays prompt instead of stretching to hundreds of milliseconds on batched Windows adapters.
- One missing reliable block may not hold all later data indefinitely. After 2.4 s or 96 pending blocks, the GUI records the true ADS sequence gap and releases later blocks immediately.
- The watchdog no longer intentionally disconnects a still-connected BLE link because DATA temporarily stopped progressing. Actual link disconnects still auto-reconnect.
- Saturated channels are isolated only in the live filter/screen copy. Raw 48-byte BIN data and ADS sequence values remain untouched.
- BLE and Serial sequence-gap verdicts are separated. BLE mode no longer tells the user to inspect Serial worker/OS buffer.
- Channel configuration no longer clears retained BLE DATA queues, and the expected post-configuration sequence restart gets a fresh baseline.
- Optional V17 BLE firmware increases reliable retention from 320 to 384 blocks and reduces periodic STATUS radio traffic while streaming.
