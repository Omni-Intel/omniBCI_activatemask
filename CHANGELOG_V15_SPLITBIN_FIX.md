# V15 Split-BIN Fix

- Restores the automatic one-minute raw BIN segmentation that existed in V9/V10.
- Naming format: `MMDD_HHMM_ID_minuteNN.bin`.
- Each complete segment is exactly 60 seconds at 250 SPS: 720,000 bytes / 15,000 frames.
- Creates a session manifest (`*_manifest.json`) and a sidecar (`*.meta.json`) for every segment.
- Segmentation, file rotation, flush and JSON metadata all run in the existing background writer thread.
- BLE receive, filtering, Qt painting and V15 saturation protection are unchanged.
- No firmware update is required.
