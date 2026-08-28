# ESP32-C3 ADS1299 independent full-differential V20

Open `ESP32C3_ADS1299_FULL_DIFF_BLE_V20.ino` directly as an Arduino sketch.
This build preserves the V19 GPIO, software-SPI, DRDY, 48-byte frame, CRC,
USB command and reliable BLE transport implementation while changing the
ADS1299 analog-input policy to true per-channel differential acquisition.

Normal acquisition invariants:

- CH1-CH8 independently measure `INxP - INxN`.
- Every `CHnSET.SRB2` bit is zero.
- `MISC1.SRB1` is zero.
- The enabled/logical-BIAS mask is written to both `BIAS_SENSP` and
  `BIAS_SENSN` in modes 0 and 1.
- The lead-off mask is written to both `LOFF_SENSP` and `LOFF_SENSN` in normal
  input modes.
- Shorted and internal-test modes retain their original MUX values and also
  keep both SRB paths disabled.

Firmware identity:

- BLE name: `OmniBCI-C3-FULLDIFF-V20`
- BLE HELLO version: `V20.0.0`, protocol V1, full-differential capability bit
- USB query: one byte `0xAB`; response is the common 12-byte XOR-protected ACK
  containing profile, semantic version, protocol and capabilities

For direct USB COM control, use `USB CDC On Boot = Enabled`. BLE-only builds do
not require that option.
