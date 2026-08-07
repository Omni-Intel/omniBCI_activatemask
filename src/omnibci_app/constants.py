
"""Application-wide protocol, timing, and presentation constants."""

from __future__ import annotations

import sys
from pathlib import Path

FS = 250
CHANNELS = 8
MNE_CHANNEL_TYPE = "eeg"
BAUD = 921600
FRAME_BYTES = 48
BYTES_PER_SECOND = FRAME_BYTES * FS
# Windows may deliver several BLE notifications in one burst.  The important
# rule is not to parse an arbitrarily large burst in a single Qt callback.
# Each transport turn handles only a bounded number of complete frames, then
# yields back to Qt.  Unprocessed bytes remain in the worker queue: no sample is
# clipped, rescaled, discarded, or replaced.
# V16 keeps two independent host schedulers.  The proven P0P1 serial path
# drains aggressively before paint work, while BLE keeps V8's bounded batches
# and jitter buffer.  Sharing one compromise scheduler was the main reason the
# V8/V9 GUI could report serial sequence gaps even when BLE looked smooth.
SERIAL_POLL_INTERVAL_MS = 4
BLE_POLL_INTERVAL_MS = 4
SERIAL_PLOT_INTERVAL_MS = 80
# Twenty visual updates per second keep the live cursor responsive while the
# receive, filtering and BIN writer remain isolated from Qt. Receive, filtering and painting are isolated; live painting is never intentionally paused by backlog.
BLE_PLOT_INTERVAL_MS = 50
# V16: the OS serial driver is drained by a dedicated reader thread.  Qt only
# consumes a RAM queue, so Windows timer jitter, window dragging, PSD work or a
# slow GPU cannot overflow the USB driver.  The timer therefore no longer has
# to hit a fragile 2 ms deadline.
SERIAL_RX_BUFFER_BYTES = 1024 * 1024
SERIAL_READER_TIMEOUT_S = 0.025
SERIAL_READER_MAX_READ_BYTES = 64 * 1024
SERIAL_HOST_MAX_QUEUE_BYTES = 8 * 1024 * 1024
SERIAL_MAX_PROCESS_FRAMES = 128
SERIAL_MAX_PROCESS_BYTES = FRAME_BYTES * SERIAL_MAX_PROCESS_FRAMES
SERIAL_REPOLL_DELAY_MS = 1
# BLE notifications normally contain four ADS frames.  Do not run the complete
# parser/timeline/filter pipeline once per notification.  Coalesce roughly
# 24-32 ms of EEG, or flush after the hold timeout, then process one matrix.
BLE_COALESCE_MIN_FRAMES = 8
BLE_COALESCE_MIN_BYTES = FRAME_BYTES * BLE_COALESCE_MIN_FRAMES
BLE_COALESCE_MAX_HOLD_S = 0.025
BLE_MAX_PROCESS_FRAMES = 96
BLE_MAX_PROCESS_BYTES = FRAME_BYTES * BLE_MAX_PROCESS_FRAMES
# V16 continuity fix: transport/recording are already isolated from Qt, so
# backlog must never deliberately freeze the waveform. These are retained only
# as diagnostic thresholds; paint is no longer paused when they are exceeded.
BLE_PLOT_PAUSE_BACKLOG_BYTES = int(BYTES_PER_SECOND * 0.50)
BLE_PSD_PAUSE_BACKLOG_BYTES = int(BYTES_PER_SECOND * 0.30)
TRANSPORT_MAX_BATCH_FRAMES = 24
TRANSPORT_MAX_BATCH_BYTES = FRAME_BYTES * TRANSPORT_MAX_BATCH_FRAMES
TRANSPORT_NORMAL_BUDGET_S = 0.0025
TRANSPORT_CATCHUP_BUDGET_S = 0.0050
TRANSPORT_REPOLL_DELAY_MS = 2
TRANSPORT_CATCHUP_THRESHOLD_BYTES = FRAME_BYTES * 10
BLE_DEVICE_NAME_SRB1 = "OmniBCI-C3-SRB1-V3"
BLE_DEVICE_NAME_SRB2 = "OmniBCI-C3-SRB2"
BLE_DEVICE_NAME_COMMON = "OmniBCI-C3-ADS1299"
BLE_DEVICE_NAMES = (
    BLE_DEVICE_NAME_SRB1,
    BLE_DEVICE_NAME_SRB2,
    "OmniBCI-C3-SRB1-V11",
    "OmniBCI-C3-SRB2-V11",
    BLE_DEVICE_NAME_COMMON,
)
# Fallback label only. Connection compatibility is verified from GATT UUIDs and
# the A7 register-readback reference byte, not from the advertised name alone.
BLE_DEVICE_NAME = BLE_DEVICE_NAME_COMMON
BLE_SERVICE_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0001"
BLE_DATA_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0002"
BLE_CONTROL_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0003"
BLE_STATUS_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0004"
BLE_BLOCK_MAGIC = b"\xB1\x4B"
BLE_BLOCK_VERSION_V1 = 1
BLE_BLOCK_VERSION_V2 = 2
BLE_BLOCK_HEADER_BYTES = 20
BLE_BLOCK_CRC_BYTES = 2
BLE_COMPACT_FRAME_BYTES = 36
BLE_V1_FRAMES_PER_BLOCK = 4
BLE_V2_FRAMES_PER_BLOCK = 6
BLE_BLOCK_MAX_PAYLOAD_BYTES = max(
    FRAME_BYTES * BLE_V1_FRAMES_PER_BLOCK,
    BLE_COMPACT_FRAME_BYTES * BLE_V2_FRAMES_PER_BLOCK,
)
BLE_CTRL_MAGIC = b"\xBA\x43"
BLE_CTRL_VERSION = 1
BLE_CTRL_ACK = 1
BLE_CTRL_NACK_RANGE = 2
BLE_CTRL_RESET = 3
BLE_CTRL_PACKET_BYTES = 18
BLE_RELIABLE_ACK_EVERY_BLOCKS = 3
BLE_RELIABLE_ACK_MAX_INTERVAL_S = 0.08
BLE_RELIABLE_NACK_REPEAT_S = 0.12
BLE_RELIABLE_MAX_PENDING_BLOCKS = 384
# Long-run continuity policy: a protocol hole may not hold the entire live
# stream hostage for many seconds. Keep asking for repair, but if future blocks
# accumulate behind one missing block, fail open before the ESP32 retention
# ring can overflow. The ADS sample sequence still records the exact real gap.
BLE_RELIABLE_FORCE_SKIP_PENDING = 96
BLE_RELIABLE_HOLE_FAILOPEN_S = 2.4
# Never intentionally disconnect a healthy GATT link just because ordered DATA
# has paused. On long sleep recordings, proactive reconnects look like app
# restarts and can reset the acquisition session. Actual BLE disconnects still
# use the normal reconnect path.
BLE_PROACTIVE_RECONNECT_ENABLED = False
# Cross-PC adaptive reliable timing.  Windows Bluetooth stacks vary widely:
# some deliver notifications every ~25 ms, others batch them for hundreds of
# milliseconds.  Fixed 100/180 ms repair timers can misclassify delivery jitter
# as packet loss and create a retransmission storm.  V16 learns the recent DATA
# notify-gap distribution and stretches ACK/NACK/reconnect timing only when the
# adapter actually needs it.
BLE_ADAPTIVE_GAP_SAMPLES = 256
BLE_ADAPTIVE_LEARN_MAX_GAP_S = 1.50
BLE_ADAPTIVE_ACK_MAX_S = 0.35
BLE_ADAPTIVE_NACK_MAX_S = 0.80
BLE_ADAPTIVE_HOLE_RECONNECT_MAX_S = 8.0
BLE_ADAPTIVE_STALL_RECONNECT_MAX_S = 12.0
BLE_ADAPTIVE_HOLE_TIMEOUT_MAX_S = 30.0
# A lost protocol block must not stop the stream forever.  NACKs are repeated
# independently of new DATA notifications; after a bounded wait the host skips
# only the missing protocol block.  The next ADS frame sequence still exposes
# the real sample loss, so the timeline receives NaNs rather than invented EEG.
BLE_RELIABLE_WATCHDOG_INTERVAL_S = 0.10
BLE_RELIABLE_HOLE_RECONNECT_S = 2.5
BLE_RELIABLE_HOLE_TIMEOUT_S = 12.0
BLE_RELIABLE_STALL_RECONNECT_S = 3.0
BLE_RELIABLE_RECONNECT_COOLDOWN_S = 6.0
BLE_MIN_STREAM_MTU = 100
# Raw BIN writes are intentionally off the Qt thread.  Windows Defender, disk
# cache flushes and removable drives can occasionally block a write long enough
# to starve serial/BLE receive and painting.
RAW_WRITER_QUEUE_CHUNKS = 2048
RAW_WRITER_BUFFER_BYTES = 1024 * 1024
RAW_WRITER_FLUSH_INTERVAL_S = 2.0
BYTES_PER_SECOND = FRAME_BYTES * FS
RECORD_SEGMENT_SECONDS = 60
RECORD_SEGMENT_BYTES = BYTES_PER_SECOND * RECORD_SEGMENT_SECONDS
RECORD_METADATA_SCHEMA = "omni_ads1299_recording_meta_v1"
RECORD_METADATA_UPDATE_INTERVAL_S = 5.0
# Live causal filtering runs in its own worker.  The Qt thread only updates
# counters/ring buffers and paints already-computed arrays.  This mirrors the
# acquisition-ring-buffer-consumer split used by BrainFlow/OpenBCI.
FILTER_RESULT_POLL_MS = 3
FILTER_RESULT_BUDGET_S = 0.003
FILTER_OUTPUT_MAX_BATCHES = 256
FILTER_BACKLOG_PAUSE_PLOT_S = 2.0  # diagnostic only; live paint is never intentionally paused
FILTER_BACKLOG_PAUSE_PSD_S = 0.30
PSD_LIVE_REFRESH_MS = 1500
PSD_LIVE_WINDOW_S = 6.0
# ADS rail samples are not useful EEG and can create a pathological Qt paint
# path when a disconnected electrode toggles rapidly between positive and
# negative full scale. Raw BIN bytes stay untouched; only the live filter and
# screen paths mask rail samples. This keeps one saturated channel from
# poisoning the IIR state or freezing all eight plots.
ADC_SATURATION_FRACTION = 0.95
# Saturation is a signal-quality condition, not a scheduling condition. PSD
# keeps running on the exact finite samples; it is never used to stop live work.
PSD_SATURATION_SKIP_RATIO = 1.01
# STATUS is already subscribed. Avoid active GATT reads while EEG is streaming,
# because Windows may serialize them with DATA notification delivery.
BLE_STATUS_POLL_INTERVAL_S = 15.0
LIVE_CATCHUP_THRESHOLD_S = 0.20
# Display-only adaptive jitter buffer. Raw parsing/saving and the filtered
# history remain complete. The live cursor may skip stale screen history only
# after a large OS delivery stall, keeping the visible delay within its budget.
# V16 retains the 0.65 s target and caps the live screen near 1 s; raw BIN is independent.
DISPLAY_JITTER_BASE_TARGET_S = 0.72
DISPLAY_JITTER_STARTUP_S = 0.62
DISPLAY_JITTER_MARGIN_S = 0.22
DISPLAY_JITTER_MAX_TARGET_S = 0.95
DISPLAY_JITTER_MIN_RESERVE_S = 0.02
DISPLAY_JITTER_LOW_RESERVE_RATE = 0.45
DISPLAY_JITTER_MAX_DT_S = 0.12
DISPLAY_JITTER_LONG_GAP_S = 0.08
# Low-latency live view: raw BIN and filtered history remain complete, while
# the screen cursor is allowed to catch up faster than real time after a Windows
# BLE burst. If stale screen history exceeds the accepted one-second budget,
# only the display cursor is resynchronised; recorded samples are untouched.
DISPLAY_JITTER_CATCHUP_TRIGGER_S = 0.10
DISPLAY_JITTER_CATCHUP_MAX_RATE = 1.75
DISPLAY_JITTER_HARD_MAX_S = 1.00
DISPLAY_JITTER_HARD_HYSTERESIS_S = 0.12
# Missing BLE frames are represented as invalid timeline samples in the live
# display ring. This preserves real sample time and prevents packet loss from
# slowly draining the jitter buffer. Raw BIN bytes remain untouched.
LIVE_TIMELINE_MAX_FILL_S = 30.0
LIVE_TIMELINE_MAX_FILL_SAMPLES = int(round(LIVE_TIMELINE_MAX_FILL_S * FS))
SYNC1 = 0xA5
SYNC2 = 0x5A
VREF = 4.5
VALID_GAINS = [1, 2, 4, 6, 8, 12, 24]
LEAD_OFF_FREQUENCY_HZ = FS / 8.0
LEAD_OFF_CURRENT_NA = 6.0
LEAD_OFF_SERIES_SRB1_KOHM = 9.98
LEAD_OFF_SERIES_SRB2_KOHM = 4.40
REFERENCE_SRB1 = 0
REFERENCE_SRB2 = 1
REFERENCE_ITEMS = [
    ("SRB2 公共参考（信号接 INxN）", REFERENCE_SRB2),
    ("SRB1 全局参考（信号接 INxP）", REFERENCE_SRB1),
]
MODE_ITEMS = [
    ("EEG + BIAS P+N", b"n", 0),
    ("EEG + BIAS 仅信号侧", b"p", 1),
    ("EEG + BIAS off", b"o", 2),
    ("ADS internal short", b"q", 3),
    ("ADS internal test square", b"t", 4),
]
MODE_NAMES = {
    0: "EEG/BIAS P+N",
    1: "EEG/BIAS signal-side",
    2: "EEG/BIAS-off",
    3: "SHORTED",
    4: "TEST",
}
# PyInstaller-safe paths.  Bundled resources live under sys._MEIPASS, while
# user recordings must stay next to the executable instead of inside the
# temporary extraction/_internal directory.
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_DIR)).resolve()
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else PROJECT_DIR
ASSET_DIR = RESOURCE_DIR / "assets"
RECORDINGS_DIR = APP_DIR / "recordings"
LOGO_PATH = ASSET_DIR / "omni_logo_cnen.png"
APP_ICON_PATH = ASSET_DIR / "omni_logo_mark.png"
OMNI_ORANGE = "#ff5a01"
OMNI_ORANGE_DARK = "#c94700"
OMNI_BLACK = "#080808"
OMNI_PAPER = "#f6f7f9"
CHANNEL_COLORS = [
    "#7B61FF", "#2478FF", "#00A6D6", "#00A878",
    "#8EBB2A", "#E0A800", "#F47A22", "#E84545",
]
