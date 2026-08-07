# Changelog

This file consolidates the release notes for OmniBCI V14 through V18. Releases are listed from newest to oldest.

## V18 — BLE capture/TX isolation

V18 targets the long-run failure shown by the diagnostic snapshot where `Sequence gaps=771`, firmware `queue_drop=771`, `notify_error=238`, and max Notify gap was about 5.1 s while GUI/filter/raw queues remained empty.

### Firmware

- Split BLE DATA notification out of `transportTask` into a dedicated low-priority `bleTxTask`.
- `transportTask` priority is raised and now owns frameQueue -> compact block -> reliable retention only; it never performs DATA notify during continuous acquisition.
- A Windows/BLE stack pause can therefore fill the ~9.2 s reliable retention ring without starving the 250 SPS ADS frameQueue.
- Periodic STATUS notify is also handed to the BLE TX task so STATUS traffic cannot stall frameQueue draining.
- Reliable ring slots publish `valid=true` only after the complete payload has been copied, making pack/TX task overlap safe.
- Reliable reset briefly suspends the TX task to avoid resetting a block while it is being copied for transmission.
- Stopping a stream no longer calls the DATA transmitter from a second task; BLE TX has one owner only.

### GUI

- BLE gap attribution now uses the full 32-bit firmware `queue_drop` STATUS counter when available. The old 8-bit in-frame hint can wrap during a large burst and incorrectly label real MCU frameQueue loss as host loss.
- Diagnostic label changed from `FW queue/notify` to `FW frameQ/notifyErr`.
- Verdict now explicitly distinguishes MCU frameQueue loss from host reliable/decode loss.

### Compatibility

The BLE reliable wire protocol remains unchanged (compact DATA protocol V2 / STATUS V4), so the V18 GUI remains compatible with the prior V16/V17 reliable firmware. For this continuity fix, use the bundled V18 firmware.

### Stale-NACK / long-run diagnostic hotfix

- GUI revalidates each queued NACK immediately before the GATT write. If the missing block has already arrived or the cumulative ACK has advanced past the requested range, the stale NACK is suppressed locally.
- Duplicate/older cumulative ACK writes are coalesced before they reach Windows BLE.
- Reliable ACK/NACK controls use write-without-response; RESET still uses write-with-response. The control characteristic already advertises both WRITE and WRITE_NR.
- Firmware no longer counts a control from an old session, or a NACK overtaken by a cumulative ACK, as a protocol error/unknown NACK.
- A cumulative ACK now cancels or trims a queued firmware NACK range immediately so stale repair traffic cannot consume BLE TX slots.
- `reliable_unknown_nack` is now reserved for a genuinely unacked, already-produced block that is absent from retention.
- GUI diagnostics show `Stale ctrl suppressed` and `FW recent o/u/p` (new overflow / unknown NACK / protocol errors since the previous STATUS update).
- Historical nonzero unknown/protocol counters no longer permanently force the red "protocol mismatch" verdict for the rest of a long recording.

## V17 — Long-run continuity

This revision targets long BLE recordings where the waveform could pause, then burst forward, especially during BIAS drift or ADC saturation.

- Bleak DATA callback now only timestamps and enqueues bytes; reliable CRC/reassembly/compact expansion runs in a dedicated decoder thread.
- Reliable ACK cadence stays prompt instead of stretching to hundreds of milliseconds on batched Windows adapters.
- One missing reliable block may not hold all later data indefinitely. After 2.4 s or 96 pending blocks, the GUI records the true ADS sequence gap and releases later blocks immediately.
- The watchdog no longer intentionally disconnects a still-connected BLE link because DATA temporarily stopped progressing. Actual link disconnects still auto-reconnect.
- Saturated channels are isolated only in the live filter/screen copy. Raw 48-byte BIN data and ADS sequence values remain untouched.
- BLE and Serial sequence-gap verdicts are separated. BLE mode no longer tells the user to inspect Serial worker/OS buffer.
- Channel configuration no longer clears retained BLE DATA queues, and the expected post-configuration sequence restart gets a fresh baseline.
- Optional V17 BLE firmware increases reliable retention from 320 to 384 blocks and reduces periodic STATUS radio traffic while streaming.

## V16 Continuity Fix

Priority order for live acquisition:

1. receive and preserve every transport frame;
2. keep the live waveform moving continuously;
3. run filtering;
4. update PSD/quality metrics only with spare CPU.

### Saturation

- Saturated ADC samples are no longer converted to NaN before the live causal filter.
- Raw BIN, sequence counters, filter input and PSD all keep the real finite rail samples.
- The screen copy is clipped to each channel's visible y-range immediately before painting. This prevents full-scale rail toggling from creating huge QPainter paths while keeping the waveform continuous.
- PSD does not pause merely because a channel is saturated.

### PSD isolation

- Live PSD uses a fixed-cost path: one raw Welch plus one filtered Welch on the newest six seconds.
- The many-overlapping-4-second quality-window loop is retained only for the richer offline/capture path.
- PSD owns a private one-thread QThreadPool and remains strictly single-flight. If one calculation is still running, the next timer tick is ignored rather than queued.
- Live PSD refresh is 1.5 s. This is intentionally lower priority than transport and waveform continuity.

### BLE waveform continuity

- Removed the old behavior that deliberately paused painting when BLE/filter backlog crossed a threshold.
- Removed the full rebuffer wait that stopped the waveform until the jitter reserve refilled.
- BLE live display now slows smoothly when its reserve is low and resumes immediately when new data arrives.
- Target display reserve is ~0.72 s and capped below ~0.95 s.
- If the radio truly delivers no new EEG samples after the reserve is exhausted, software cannot invent missing samples; however the GUI no longer adds an extra refill freeze after data resumes.

### Rendering

- All live channel curves use clip-to-view and automatic peak downsampling.
- Rail values are clipped only on the paint copy; recorded/analysed values are unchanged.

### Firmware

This is a GUI-only continuity update. Keep the V16 BLE firmware. No reflash is required if V16 firmware is already installed.

## V16 — Cross-PC adaptation

V16 is based on V15_SPLITBIN_FIX and keeps its saturation protection, 60-second BIN segmentation, background BIN writer, filter worker, low-latency display and reliable compact BLE protocol.

### USB serial

- Added a dedicated `SerialTransportWorker` that continuously drains the Windows serial driver outside the Qt event loop.
- Qt now consumes a RAM queue in bounded batches. Window dragging, plotting, PSD work or GPU stalls no longer have to meet a 2 ms serial polling deadline.
- Configuration ACK reading is isolated from the normal EEG parser, removing an ACK-consumption race.
- The requested 1 MB Windows serial RX buffer is now reported in diagnostics instead of silently ignoring failure.

### BLE host

- DATA notify gaps are learned per computer/adapter using a rolling gap distribution and EWMA.
- ACK interval, ACK block cadence, repeated NACK interval, hole reconnect, stall reconnect and emergency timeout adapt to the observed Windows BLE delivery pattern.
- Slow/batched adapters therefore no longer trigger a 100 ms NACK storm merely because Windows delivered notifications late.

### BLE firmware

- Replaced the fixed 180 ms automatic retry timeout with an ACK-driven adaptive timeout (initial 1200 ms, bounded 800-3000 ms).
- Explicit host NACK repair still has priority.
- Automatic timeout retransmission occurs only after ACK progress has really stalled.
- BLE TX pacing adapts to in-flight occupancy (9/14/20 ms) to reduce burst pressure while preserving catch-up ability.

### Portable install

- `install_and_run.bat` creates and always uses a local `.venv`.
- Prefers Python 3.12/3.11 when available.
- Live acquisition no longer requires `mne` or `pyedflib`.
- Optional export packages moved to `requirements_optional_export.txt` and `install_optional_exports.bat`.
- Added `diagnose_environment.bat`.

### Important

Electrical USB noise is not a GUI path issue. If noise changes strongly between charger/USB-isolator/BLE-battery tests, investigate USB ground/common-mode coupling separately.

### EXE packaging readiness

- Added PyInstaller-safe resource/data paths (`sys._MEIPASS` for bundled assets, executable directory for recordings).
- Added `OmniBCI_V16.spec` and `build_exe.bat` for a Windows x64 ONEDIR build.
- Added GitHub Actions Windows build workflow.
- BDF/FIF-only `mne` and `pyedflib` are excluded from the live EXE to keep deployment reliable across PCs.

## V15 Split-BIN Fix

- Restores the automatic one-minute raw BIN segmentation that existed in V9/V10.
- Naming format: `MMDD_HHMM_ID_minuteNN.bin`.
- Each complete segment is exactly 60 seconds at 250 SPS: 720,000 bytes / 15,000 frames.
- Creates a session manifest (`*_manifest.json`) and a sidecar (`*.meta.json`) for every segment.
- Segmentation, file rotation, flush and JSON metadata all run in the existing background writer thread.
- BLE receive, filtering, Qt painting and V15 saturation protection are unchanged.
- No firmware update is required.

## V15 — 饱和稳定性修复

V15 保留 V14 的低延迟 BLE、多线程滤波和后台 BIN 写盘，只处理 ADS1299 输入饱和时的 GUI 卡死问题。

### 修复内容

- 每个通道独立检测接近 ADS1299 正/负满量程的样本。
- 原始 ring buffer、sequence 统计和 BIN 文件完全不改动。
- 饱和样本只在实时滤波副本中变成 NaN；滤波器用上一有效值维持状态，避免轨到轨输入污染 IIR。
- 绘图前再次根据原始数据屏蔽饱和位置，避免 pyqtgraph/QPainter 绘制数千条正负满量程竖线并占满 Qt 主线程。
- 一个通道饱和时只让该通道出现空白段，其他通道继续刷新。
- 选中的通道饱和比例达到 1% 时暂停 PSD，恢复后自动继续。
- 状态栏显示当前被饱和保护的通道，原始 BIN 仍然保留饱和数据。

### 固件兼容性

本版本没有修改 BLE 数据协议或 ESP32 固件。板上已有可用的 V13/V14 BLE 固件时，不需要重新烧录。

## V14 — Low-latency changes

V14 keeps V13's separate BLE receive, raw BIN writer, live filter worker and Qt paint paths, but reduces display latency.

- BLE GUI poll: 8 ms -> 4 ms.
- BLE coalescing: 16 frames / 60 ms -> 8 frames / 25 ms.
- Waveform paint: 10 FPS -> 20 FPS.
- Filter result poll: 5 ms -> 3 ms.
- Wireless display reserve: 2.0 s -> 0.65 s target, 0.55 s startup, adaptive cap 1.0 s.
- The display cursor accelerates up to 1.75x after a delivery burst.
- If screen history becomes older than about 1.12 s, only the live display cursor resynchronises to the 0.65 s target. Raw BIN bytes, sequence accounting and filtered ring data are not deleted.

The compact reliable BLE V2 wire protocol is unchanged from V13. A board already running V13 firmware does not need to be reflashed for the V14 GUI.
