# OpenBCI Cyton firmware reference notes

## Local source

- Reference repo: `D:\OpenBCI_Cyton_Library`
- Key files:
  - `OpenBCI_32bit_Library.cpp`
  - `OpenBCI_32bit_Library.h`
  - `OpenBCI_32bit_Library_Definitions.h`

## What is worth copying

1. Command style
   - `1..8` disables channels 1..8.
   - `! @ # $ % ^ & *` enables channels 1..8.
   - `b`, `s`, and `?` are start, stop, and query style commands.

2. Stream-safe register changes
   - OpenBCI stops streaming before changing ADS1299 channel registers.
   - It restores streaming afterward if the board was streaming before the command.
   - Our GUI keeps the safer host-side version: send `s`, send `MHH\n`, wait for ACK, then send `b` only if needed.

3. State mirror concept
   - OpenBCI keeps `channelSettings[][]`, `useInBias[]`, and `useSRB2[]` as firmware-side mirrors of ADS1299 register state.
   - Our v1 intentionally keeps this minimal with `activeMask`; if we add gain/input-mode/lead-off later, we should add a small state mirror rather than scattering raw register writes.

4. Bias bit maintenance
   - OpenBCI explicitly removes a disabled channel from `BIAS_SENSP`/`BIAS_SENSN`.
   - Our core requirement is the same: unused channels must not enter Bias calculation.

## What must not be copied directly

1. Front-end topology
   - OpenBCI default channel settings use SRB2 and not SRB1 by default.
   - Our `gaoboV2.net` uses SRB1 as the common reference side, so `MISC1=0x20` is intentional.

2. Bias P/N behavior
   - OpenBCI commonly updates both `BIAS_SENSP` and `BIAS_SENSN` for a channel.
   - Our board should use P-only Bias for the active EEG channels:
     - `BIAS_SENSP = activeMask`
     - `BIAS_SENSN = 0x00`

3. Channel connector order
   - Our connector order comes from `gaoboV2.net`, not OpenBCI:
     - `H1-1 = BIAS_OUT`
     - `H1-2 = AIN8P`
     - `H1-3 = AIN7P`
     - `H1-4 = AIN6P`
     - `H1-5 = AIN5P`
     - `H1-6 = AIN4P`
     - `H1-7 = AIN3P`
     - `H1-8 = AIN2P`
     - `H1-9 = AIN1P`
     - `H1-10 = AREF`

4. Disabled channel register value
   - OpenBCI disables by setting power-down and clearing SRB2/Bias bits.
   - Our v1 uses `CHnSET=0xE1` for inactive channels: power-down + gain24 + input short.
   - This is more direct for the current problem: floating or unused electrode channels must not disturb the Bias loop.

## Current gaobo v1 mapping

- `activeMask` bit mapping:
  - bit0 = CH1 = AIN1P = H1-9
  - bit1 = CH2 = AIN2P = H1-8
  - bit2 = CH3 = AIN3P = H1-7
  - bit3 = CH4 = AIN4P = H1-6
  - bit4 = CH5 = AIN5P = H1-5
  - bit5 = CH6 = AIN6P = H1-4
  - bit6 = CH7 = AIN7P = H1-3
  - bit7 = CH8 = AIN8P = H1-2

- Enabled channel:
  - `CHnSET = 0x60`

- Disabled channel:
  - `CHnSET = 0xE1`

- Normal Bias:
  - `BIAS_SENSP = activeMask`
  - `BIAS_SENSN = 0x00`
  - `MISC1 = 0x20`

## Recommended next refinement

If we want to move one step closer to OpenBCI robustness without copying its board assumptions, add a tiny local state mirror:

```text
channelEnabled[8]
channelGain[8]
channelInputMode[8]
channelUseBiasP[8]
```

For the current acceptance target, `activeMask` is enough. Add the mirror only when the GUI needs per-channel gain, input mux, lead-off, or impedance checks.
