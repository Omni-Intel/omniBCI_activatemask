# P0+P1 Change Log

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
