# ONMI BCI activeMask / ADS1299 Bias Control

ESP32-C3 + ADS1299 8-channel EEG firmware and a Python PC control app.

This fork focuses on SRB1 reference acquisition and automatic `BIAS_SENSP`
maintenance. Channels can keep recording even when they are removed from the
BIAS calculation.

## Current Behavior

- ADS1299 uses SRB1 as the common reference.
- `BIAS_SENSN` is always `0x00`.
- `BIAS_SENSP` is maintained for CH1-CH5 only.
- CH6-CH8 are not forced disabled; they still use normal gain and remain in the
  binary stream.
- Starting a stream runs one lead-off check first.
- During acquisition, every 2 seconds the firmware analyzes CH1-CH5 raw data and
  updates `BIAS_SENSP` slowly.
- The 48-byte binary frame format is unchanged.

## Directory Layout

```text
firmware/
  ESP32C3_ADS1299_active_mask/
    ESP32C3_ADS1299_active_mask.ino
  ESP32C3_ADS1299_active_mask_8ch_bias/
    ESP32C3_ADS1299_active_mask_8ch_bias.ino

pc_app/
  active_mask_gui.py
  run_gui.cmd
  self_check.py
  requirements.txt

tools/
  common.ps1
  setup_pc_env.ps1
  run_gui.ps1
  package_gui.ps1
  install_esp32_core.ps1
  compile_firmware.ps1
  upload_firmware.ps1
  launch_arduino_ide_ascii_cache.ps1
```

## BIAS Mask Rules

`activeMask` now means the P-side mask used for `BIAS_SENSP`.

```text
bit0 = CH1
bit1 = CH2
bit2 = CH3
bit3 = CH4
bit4 = CH5
```

CH6-CH8 are still sampled, but they are not part of the automatic BIAS quality
decision.

```text
BIAS_SENSP = activeMask & 0x1F
BIAS_SENSN = 0x00
```

The automatic algorithm never writes a zero BIAS mask. If all CH1-CH5 look bad,
the last valid mask is kept.

## 8-Channel BIAS Variant

An alternate firmware is provided for experiments where all 8 channels may
participate in `BIAS_SENSP`:

```text
firmware/ESP32C3_ADS1299_active_mask_8ch_bias/
```

Differences from the default firmware:

```text
BIAS_CHANNEL_COUNT = 8
VALID_BIAS_MASK = 0xFF
CH1-CH8 are all analyzed every 2 seconds
Good channels can be added to BIAS_SENSP
Bad channels can be removed from BIAS_SENSP
BIAS_SENSN remains 0x00
The mask still cannot become 0
```

This variant is useful when CH6-CH8 may also be real electrodes instead of
always-unused inputs.

## Automatic Quality Masking

Every 500 samples, about 2 seconds at 250 SPS, the firmware analyzes CH1-CH5.
A channel is considered bad when any of these conditions are true:

```text
bad_saturation: abs(median(raw)) > 0.7FS or sat_ratio > 0.1%
bad_line:       48-52Hz power / 1-40Hz power > 0.3
bad_noisy:      rms_1_40 > 8x channel-median RMS and > 300 uV
bad_flat:       rms_1_40 < 0.2 uV or p2p < 1 uV
bad_drift:      adjacent-window DC jump > 0.05FS
bad_leadoff:    ADS1299 LOFF_STATP reports lead-off
```

Debounce:

```text
3 consecutive bad windows  -> remove channel from BIAS_SENSP
5 consecutive good windows -> restore channel to BIAS_SENSP
```

After a BIAS register update, the firmware discards about 1.5 seconds of frames
to avoid transient data.

## Serial Commands

Baud rate:

```text
921600
```

| Command | Meaning |
| --- | --- |
| `b` | Run one lead-off check, then start binary stream |
| `s` | Stop stream |
| `MHH\n` | Set `BIAS_SENSP` mask, for example `M1F` |
| `1..8` | Remove CH1..CH8 from the mask command path |
| `! @ # $ % ^ & *` | Add CH1..CH8 through the mask command path |
| `i` | Run one manual initial impedance/lead-off mask check |
| `?` | Stop stream and print diagnostics |
| `q` | Internal shorted-input test |
| `t` | Internal test signal |
| `o` | Bias off |
| `e` / `p` | Normal SRB1 + P-only BIAS mode |

Typical diagnostics include:

```text
activeMask=0x1F
BIAS_SENSP=0x1F BIAS_SENSN=0x00
qualityWindows=...
biasUpdates=...
lastBadMask=0x..
lastGoodMask=0x..
```

## PC App

Run the GUI from the project root:

```powershell
.\run_all.cmd
```

Or:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_gui.ps1
```

The launcher creates a project-local `.venv` and installs `pc_app\requirements.txt`.

Package the GUI as an executable:

```powershell
.\package_gui.cmd
```

Output:

```text
dist\ActiveMaskGUI\ActiveMaskGUI.exe
```

## Firmware Build and Upload

The scripts are project-local. They no longer depend on the original author's
hard-coded `D:\arduino_IDE` or conda paths.

Install ESP32 Arduino core into the project-local Arduino data folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_esp32_core.ps1
```

Compile:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\compile_firmware.ps1
```

Compile the 8-channel BIAS variant:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\compile_firmware.ps1 ESP32C3_ADS1299_active_mask_8ch_bias
```

Upload, automatically selecting a serial port:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1
```

Upload to a specific port:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1 COM4
```

Upload the 8-channel BIAS variant:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\upload_firmware.ps1 COM4 ESP32C3_ADS1299_active_mask_8ch_bias
```

## Binary Frame

The firmware keeps the original 48-byte binary frame:

```text
[0..1]   sync header 0xA5 0x5A
[2]      protocol version
[3]      frame type
[4..7]   sample sequence
[8..11]  micros()
[12..14] ADS1299 STATUS
[15]     flags
[16..39] 8 x 24-bit ADS1299 raw channel data, MSB-first
[40..45] diagnostics
[46..47] CRC16-CCITT-FALSE
```

The current BIAS mask is not embedded into the 48-byte frame. The PC app records
mask history in the JSON sidecar when saving `.bin` data.

## Notes

- Only one program can use the serial port at a time. Close Arduino Serial
  Monitor, XCOM, and other serial tools before connecting with the GUI.
- CH6-CH8 can be left recording while excluded from `BIAS_SENSP`.
- The GUI saves raw `.bin` plus `.json` metadata. It does not plot live EEG.
