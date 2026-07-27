# P0+P1 Change Log

## Automatic MNE and FIF export on BIN import

- Importing a BIN now automatically builds an unfiltered eight-channel MNE
  `RawArray` in volts at 250 Hz.
- Saves an MNE interchange CSV under `recordings/nme`.
- Saves a native double-precision `*_raw.fif` under `recordings/fif`.
- Creates both directories automatically and overwrites the matching derived
  files when the same BIN is imported again.
- Preserves invalid/CRC-bad frame runs as MNE `BAD_frame` annotations.
- The MNE browser now reuses the same conversion helper as the saved FIF.

## Power-line harmonic rejection

- Made the startup display filter match the toolbar default of 5–50 Hz.
- Replaced the single 50 Hz notch with cascaded 50 Hz and 100 Hz Q=30 notches.
- At 250 SPS, a 150 Hz component aliases to 100 Hz and is handled by the
  100 Hz section.
- Reused the same harmonic-notch chain for live filtering, imported BIN
  zero-phase filtering, the enlarged channel view and spectral analysis.

## Single-channel enlarged waveform tab

- Double-clicking any channel waveform opens it in a dedicated tab.
- The enlarged plot contains only the selected channel and follows the current
  time window, filter state and per-channel amplitude range.
- Added direct CH1-CH8 selection and live power/PGA/BIAS/reference status.
- Reused the prepared display buffer so the extra curve adds no filtering pass.

## Verified ADS1299 configuration writes

- A6/A7 writes now return a checksummed 12-byte binary acknowledgement.
- The acknowledgement contains actual ADS1299 CHnSET, BIAS_SENSP,
  BIAS_SENSN and MISC1 readback values.
- Live configuration now stops streaming before the write and resumes it only
  after validation.
- GUI state is no longer presented as hardware-confirmed when the firmware
  returns no acknowledgement or register verification fails.

## OpenBCI SRB2 front-end correction

- Corrected the project default to the documented OpenBCI montage: signals on INxN, common reference on SRB2.
- GUI now opens with SRB2/OpenBCI and BIAS P+N selected while retaining SRB1 and signal-side-only modes.
- Added `ESP32C3_ADS1299_OPENBCI_SRB2_REFERENCE` as a dedicated, non-destructive firmware entry point.
- Added a repeating READY banner until the first host command so a late-opened serial monitor is not blank.
- Preserved raw ADS polarity (`SRB2-INxN`) and left any display inversion to the GUI layer.

## Dual SRB1/SRB2 reference support

- Added a global SRB1/SRB2 hardware-reference selector.
- Clicking a channel opens independent power, PGA, BIAS and SRB2 controls when applicable.
- Channel labels continuously show `ON/OFF`, PGA, BIAS and effective reference state.
- Added the `A7 <channel> <gain> <flags>` firmware control message.
- Added `A8 <reference-mode>` with SRB1/SRB2 mutual exclusion.
- Firmware writes and verifies each CHnSET register independently.
- Raw-count conversion uses the actual gain of each channel.
- SRB1 routes BIAS through BIAS_SENSP; SRB2 routes it through BIAS_SENSN.
- Logical BIAS masks are intersected with the enabled-channel mask in both GUI and firmware.
- Input-short and internal-test modes disable both reference buses.

## Live rendering performance

- Reused the causal filtered ring instead of filtering the entire display window every repaint.
- Added batched ring-buffer writes, linked X axes, visible-range clipping and automatic downsampling.
- Increased waveform painting to 20 FPS while reducing PSD scheduling to 1 Hz.

## Omni-Intelligence visual system

- Reused the supplied Omni-Intelligence bilingual logo and application mark.
- Replaced the blue clinical theme with the Omni white, black and orange palette.
- Changed waveform and PSD canvases to signal-black with orange active traces.
- Added orange selection, navigator, tabs, status and dialog accents.

- Separated raw hardware diagnostics from filtered EEG/Alpha analysis.
- Added continuous stateful real-time SOS filtering with a dedicated filtered ring buffer.
- Replaced the old 5–50 Hz display band with 1–40 Hz plus a narrow 50 Hz Q=30 notch.
- Made Alpha processing independent of all display checkboxes.
- Added 4 s / 75% overlap spectral segments with bad-segment rejection and median PSD aggregation.
- Added dB-domain exponential PSD display smoothing (`beta=0.85`).
- Added raw RMS, filtered RMS, raw peak-to-peak, valid ratio, and quality reason reporting.
- Added 20-second open-eye/closed-eye median Alpha capture.
- Replaced deprecated `np.trapz` with `np.trapezoid`.
- Preserved the original firmware protocol, recording format, controls, and included recordings.
