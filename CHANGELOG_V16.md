# V16 cross-PC adaptation

V16 is based on V15_SPLITBIN_FIX and keeps its saturation protection, 60-second BIN segmentation, background BIN writer, filter worker, low-latency display and reliable compact BLE protocol.

## USB serial
- Added a dedicated `SerialTransportWorker` that continuously drains the Windows serial driver outside the Qt event loop.
- Qt now consumes a RAM queue in bounded batches. Window dragging, plotting, PSD work or GPU stalls no longer have to meet a 2 ms serial polling deadline.
- Configuration ACK reading is isolated from the normal EEG parser, removing an ACK-consumption race.
- The requested 1 MB Windows serial RX buffer is now reported in diagnostics instead of silently ignoring failure.

## BLE host
- DATA notify gaps are learned per computer/adapter using a rolling gap distribution and EWMA.
- ACK interval, ACK block cadence, repeated NACK interval, hole reconnect, stall reconnect and emergency timeout adapt to the observed Windows BLE delivery pattern.
- Slow/batched adapters therefore no longer trigger a 100 ms NACK storm merely because Windows delivered notifications late.

## BLE firmware
- Replaced the fixed 180 ms automatic retry timeout with an ACK-driven adaptive timeout (initial 1200 ms, bounded 800-3000 ms).
- Explicit host NACK repair still has priority.
- Automatic timeout retransmission occurs only after ACK progress has really stalled.
- BLE TX pacing adapts to in-flight occupancy (9/14/20 ms) to reduce burst pressure while preserving catch-up ability.

## Portable install
- `install_and_run.bat` creates and always uses a local `.venv`.
- Prefers Python 3.12/3.11 when available.
- Live acquisition no longer requires `mne` or `pyedflib`.
- Optional export packages moved to `requirements_optional_export.txt` and `install_optional_exports.bat`.
- Added `diagnose_environment.bat`.

## Important
Electrical USB noise is not a GUI path issue. If noise changes strongly between charger/USB-isolator/BLE-battery tests, investigate USB ground/common-mode coupling separately.

## EXE packaging readiness
- Added PyInstaller-safe resource/data paths (`sys._MEIPASS` for bundled assets, executable directory for recordings).
- Added `OmniBCI_V16.spec` and `build_exe.bat` for a Windows x64 ONEDIR build.
- Added GitHub Actions Windows build workflow.
- BDF/FIF-only `mne` and `pyedflib` are excluded from the live EXE to keep deployment reliable across PCs.
