# P0+P1 Change Log

## OpenBCI-style per-channel settings

- Clicking a channel opens independent power, PGA, BIAS_SENSP and SRB2 controls.
- Channel labels continuously show `ON/OFF`, PGA, BIAS and SRB2 state.
- Added the `A7 <channel> <gain> <flags>` firmware control message.
- Firmware writes and verifies each CHnSET register independently.
- Raw-count conversion uses the actual gain of each channel.
- SRB1 remains global because ADS1299 MISC1 does not support per-channel SRB1.

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
