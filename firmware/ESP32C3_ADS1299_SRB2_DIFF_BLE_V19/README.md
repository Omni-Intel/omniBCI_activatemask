# ESP32-C3 ADS1299 SRB2 differential V19

This is the fixed-SRB2 build for `bciband/myEMGpcb`.

- `CHnSET.SRB2=1` for every enabled normal-input channel.
- `MISC1.SRB1=0`.
- Channel polarity is `SRB2 - INxN`; connect the shared reference electrode to
  `ASRB2` and the eight measurement electrodes to the channel N inputs.
- `BIAS_SENSN` is used by the default signal-side BIAS mode.
- CH1-CH8 are enabled by default.
- Runtime sample rates are 250, 500 and 1000 SPS and use the same BLE V19
  transaction/readback mechanism as the SRB1 build.
- The 48-byte sample format, UUIDs and GUI channel controls are unchanged.

The wrapper intentionally reuses the tested V19 implementation instead of
duplicating a 90+ KB sketch. Open this folder's `.ino` in Arduino IDE and build
it as an ESP32-C3 target.
