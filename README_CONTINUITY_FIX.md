# OmniBCI V16 Continuity Fix

Run `install_and_run.bat` the first time. Later run `run.bat`.

This package is the normal Python version, not an EXE build.

The live pipeline is intentionally ordered as:

`BLE/USB receive -> raw BIN writer -> live filter -> waveform -> PSD`

Transport reception, BIN writing and filtering are independent workers. PSD is strictly single-flight and cannot build a queue. Saturation remains in the real data and PSD; only the screen copy is clipped to the visible y-range so rail toggling cannot overload Qt painting.

For BLE, the display buffer targets roughly 0.72 s and never performs the old full "rebuffer until filled" freeze. It slows when the reserve is low and resumes immediately when data arrives.

If V16 BLE firmware is already installed, do not reflash for this GUI-only fix.
