# -*- coding: utf-8 -*-
"""
ADS1299 EEG Native Python GUI
--------------------------------
Fast PyQtGraph-based replacement for the MATLAB diagnostic GUI.
Reliable BLE V18 long-run continuity transport with asynchronous one-minute BIN segmentation: bytes delivered by Windows are kept losslessly in a worker-owned queue.
Large Windows BLE delivery bursts are cooperatively parsed in bounded batches,
while a delayed playback cursor absorbs notification jitter. Raw samples, signal
amplitude, filtering options, and recording behavior remain unchanged.

Protocol expected from firmware:
  - BLE DATA accepts Reliable Block V1 and compact V2 (session id + block sequence + CRC16)
  - the BLE worker restores the original ordered 48-byte binary frame stream
  - 48-byte binary frames
  - sync: A5 5A
  - frame[2] == 1, frame[3] == 1
  - seq: bytes 4..7 little endian uint32
  - ADS status: bytes 12..14
  - flags: byte 15
  - 8 channels: bytes 16..39, signed 24-bit big endian per channel
  - read_us: bytes 40..41 little endian uint16
  - pending: byte 42
  - mode: byte 43   0=P+N, 1=P-only, 2=BIAS-off, 3=shorted, 4=test
  - queue_depth: byte 44
  - queue_drop_low: byte 45
  - crc16-ccitt over bytes 0..45, little endian at 46..47

BIAS_SENSP command sent by this app:
  A6 0D XX
where XX is the logical BIAS channel mask. Compatible firmware routes it
to BIAS_SENSP for SRB1 or BIAS_SENSN for SRB2.

Atomic eight-channel initialization command:
  A5 REFERENCE ENABLED_MASK BIAS_MASK SRB2_MASK GAIN1..GAIN8

Legacy per-channel hardware command:
  A7 CH GAIN FLAGS
where FLAGS bit0 enables the channel and bit1 includes it in the active
reference mode's BIAS summing network.
FLAGS bit2 controls the per-channel SRB2 switch.

Reference command:
  A8 MODE
where MODE 0 selects SRB1 and MODE 1 selects SRB2.
"""

from __future__ import annotations

import os
import sys
import time
import struct
import binascii
import asyncio
import json
import secrets
import queue
import threading
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Tuple

from app_diagnostics import (
    HangWatchdog,
    configure_logging,
    dump_all_thread_stacks,
    shutdown_logging,
)

from onmibci_ble_protocol import (
    MSG_GET_CONFIG,
    MSG_HELLO,
    MSG_RESPONSE,
    MSG_SET_CONFIG,
    PROTOCOL_VERSION as BLE_DEVICE_PROTOCOL_VERSION,
    ProtocolError,
    decode_config_snapshot,
    decode_packet,
    encode_packet,
    encode_set_config,
)

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing pyserial. Run: pip install pyserial") from exc

try:
    from scipy import signal
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing scipy. Run: pip install scipy") from exc

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing PySide6. Run: pip install PySide6") from exc

try:
    import pyqtgraph as pg
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing pyqtgraph. Run: pip install pyqtgraph") from exc

try:
    from bleak import BleakClient, BleakScanner
    BLE_AVAILABLE = True
    BLE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - serial mode remains usable
    BleakClient = None
    BleakScanner = None
    BLE_AVAILABLE = False
    BLE_IMPORT_ERROR = str(exc)

from onmibci_stream import (
    LocalStreamServer,
    MarkerEvent,
    STREAM_FILTERED,
    STREAM_RAW,
    bdf_annotation_for_marker,
    publish_gui_matrix,
)


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
BLE_DEVICE_NAME_SRB1 = "OmniBCI-C3-SRB1-V19"
BLE_DEVICE_NAME_COMMON = "OmniBCI-C3-ADS1299"
BLE_DEVICE_NAMES = (
    BLE_DEVICE_NAME_SRB1,
    BLE_DEVICE_NAME_COMMON,
)
# Fallback label only. Connection compatibility is verified from GATT UUIDs and
# the A7 register-readback reference byte, not from the advertised name alone.
BLE_DEVICE_NAME = BLE_DEVICE_NAME_COMMON
BLE_SERVICE_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0001"
BLE_DATA_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0002"
BLE_CONTROL_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0003"
BLE_STATUS_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0004"
BLE_RESPONSE_UUID = "79f60000-3a7d-4b11-9f4e-4c57a50d0005"
BLE_FIRMWARE_VERSION = 19
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
REFERENCE_ITEMS = [("SRB1 全局参考（信号接 INxP）", REFERENCE_SRB1)]
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
SOURCE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR)).resolve()
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else SOURCE_DIR
ASSET_DIR = RESOURCE_DIR / "assets"
RECORDINGS_DIR = APP_DIR / "recordings"
LOGO_PATH = ASSET_DIR / "omni_logo_cnen.png"
APP_ICON_PATH = ASSET_DIR / "omni_logo_mark.png"
LOG_DIR = APP_DIR / "logs"
APP_LOGGER = logging.getLogger("onmibci")
APP_LOG_PATH = LOG_DIR / "onmibci.log"
OMNI_ORANGE = "#ff5a01"
OMNI_ORANGE_DARK = "#c94700"
OMNI_BLACK = "#080808"
OMNI_PAPER = "#f6f7f9"
CHANNEL_COLORS = [
    "#7B61FF", "#2478FF", "#00A6D6", "#00A878",
    "#8EBB2A", "#E0A800", "#F47A22", "#E84545",
]


@dataclass
class Frame:
    sequence: int
    uv: np.ndarray
    valid: bool
    mode: int
    status: bytes
    flags: int
    read_us: int
    pending: int
    queue_depth: int
    queue_drop_low: int
    raw_counts: np.ndarray


def crc16_ccitt(data: bytes) -> int:
    # ``crc_hqx`` implements the same CRC-CCITT polynomial in C.  The old
    # Python bit loop was correct but became expensive when the GUI had to
    # catch up after a paint/analysis stall.
    return int(binascii.crc_hqx(data, 0xFFFF)) & 0xFFFF


def sequence_gap_size(previous_sequence: Optional[int], current_sequence: int) -> int:
    """Return a believable forward sequence gap, excluding resets/wrap artifacts."""
    if previous_sequence is None:
        return 0
    delta = (int(current_sequence) - int(previous_sequence)) & 0xFFFFFFFF
    return int(delta - 1) if 1 < delta < 1_000_000 else 0


def expand_frames_to_timeline(
    frames: List[Frame],
    previous_sequence: Optional[int],
    previous_mode: int,
    max_fill_samples: int = LIVE_TIMELINE_MAX_FILL_SAMPLES,
):
    """Expand sequence gaps into invalid samples without inventing EEG values.

    Returned NaN columns exist only in the in-memory display/analysis timeline.
    The on-disk raw BIN remains the exact received 48-byte stream. This keeps
    the live clock honest when Windows/BLE loses notifications: the graph shows
    a visible data gap instead of compressing time or exhausting its buffer.
    """
    if not frames:
        return (
            np.empty((CHANNELS, 0), dtype=np.float32),
            np.empty(0, dtype=bool),
            np.empty(0, dtype=np.uint32),
            np.empty(0, dtype=np.uint8),
            0, 0, 0, 0, previous_sequence, previous_mode,
        )

    values_parts = []
    valid_parts = []
    sequence_parts = []
    mode_parts = []
    lost_samples = 0
    filled_samples = 0
    gap_events = 0
    large_discontinuities = 0
    prev_seq = previous_sequence
    prev_mode = int(previous_mode)

    for frame in frames:
        if prev_seq is not None:
            delta = (int(frame.sequence) - int(prev_seq)) & 0xFFFFFFFF
            if 1 < delta < 1_000_000:
                gap = int(delta - 1)
                lost_samples += gap
                gap_events += 1
                if gap <= int(max_fill_samples):
                    gap_seq = (
                        np.arange(1, gap + 1, dtype=np.uint64) + np.uint64(prev_seq)
                    ) & np.uint64(0xFFFFFFFF)
                    values_parts.append(
                        np.full((CHANNELS, gap), np.nan, dtype=np.float32)
                    )
                    valid_parts.append(np.zeros(gap, dtype=bool))
                    sequence_parts.append(gap_seq.astype(np.uint32))
                    mode_parts.append(np.full(gap, prev_mode, dtype=np.uint8))
                    filled_samples += gap
                else:
                    large_discontinuities += 1

        values_parts.append(np.asarray(frame.uv, dtype=np.float32).reshape(CHANNELS, 1))
        valid_parts.append(np.array([bool(frame.valid)], dtype=bool))
        sequence_parts.append(np.array([frame.sequence], dtype=np.uint32))
        mode_parts.append(np.array([frame.mode], dtype=np.uint8))
        prev_seq = int(frame.sequence)
        prev_mode = int(frame.mode)

    return (
        np.concatenate(values_parts, axis=1),
        np.concatenate(valid_parts),
        np.concatenate(sequence_parts),
        np.concatenate(mode_parts),
        lost_samples,
        filled_samples,
        gap_events,
        large_discontinuities,
        prev_seq,
        prev_mode,
    )


class AdsFrameParser:
    def __init__(self, get_lsb_uv):
        self.buf = bytearray()
        self.get_lsb_uv = get_lsb_uv
        self.crc_bad = 0
        self.sync_drop = 0

    def reset(self):
        self.buf.clear()
        self.crc_bad = 0
        self.sync_drop = 0

    def feed(self, data: bytes) -> List[Frame]:
        if data:
            self.buf.extend(data)
        out: List[Frame] = []

        while len(self.buf) >= 2:
            idx = self.buf.find(bytes([SYNC1, SYNC2]))
            if idx < 0:
                # Keep last byte in case it is A5 and next packet starts with 5A.
                if len(self.buf) > 1:
                    self.sync_drop += len(self.buf) - 1
                    del self.buf[:-1]
                return out
            if idx > 0:
                self.sync_drop += idx
                del self.buf[:idx]
            if len(self.buf) < FRAME_BYTES:
                return out

            frame = bytes(self.buf[:FRAME_BYTES])
            if frame[2] != 1 or frame[3] != 1:
                del self.buf[0]
                self.sync_drop += 1
                continue

            rx_crc = frame[46] | (frame[47] << 8)
            calc_crc = crc16_ccitt(frame[:46])
            if rx_crc != calc_crc:
                self.crc_bad += 1
                # Slide one byte to re-sync, just like the MATLAB parser.
                del self.buf[0]
                continue

            del self.buf[:FRAME_BYTES]
            out.append(self._decode(frame))

        if len(self.buf) > 500_000:
            del self.buf[:-1000]
        return out

    def _decode(self, frame: bytes) -> Frame:
        seq = struct.unpack_from("<I", frame, 4)[0]
        status = frame[12:15]
        flags = frame[15]
        valid = bool(flags & 0x01) and bool(flags & 0x02)
        counts = np.zeros(CHANNELS, dtype=np.int32)
        for ch in range(CHANNELS):
            i = 16 + ch * 3
            v = (frame[i] << 16) | (frame[i + 1] << 8) | frame[i + 2]
            if v & 0x800000:
                v -= 0x1000000
            counts[ch] = v
        uv = counts.astype(np.float32) * np.float32(self.get_lsb_uv())
        read_us = struct.unpack_from("<H", frame, 40)[0]
        return Frame(
            sequence=seq,
            uv=uv,
            valid=valid,
            mode=frame[43],
            status=status,
            flags=flags,
            read_us=read_us,
            pending=frame[42],
            queue_depth=frame[44],
            queue_drop_low=frame[45],
            raw_counts=counts,
        )


class RingBuffer:
    def __init__(self, channels: int, capacity: int):
        self.channels = channels
        self.capacity = capacity
        self.data = np.full((channels, capacity), np.nan, dtype=np.float32)
        self.valid = np.zeros(capacity, dtype=bool)
        self.seq = np.zeros(capacity, dtype=np.uint32)
        self.mode = np.zeros(capacity, dtype=np.uint8)
        self.head = 0
        self.count = 0
        self.total_appended = 0

    def clear(self):
        self.data.fill(np.nan)
        self.valid.fill(False)
        self.seq.fill(0)
        self.mode.fill(0)
        self.head = 0
        self.count = 0
        self.total_appended = 0

    def append(self, frame: Frame):
        self.append_values(frame.uv, frame.valid, frame.sequence, frame.mode)

    def append_values(self, values: np.ndarray, valid: bool, sequence: int, mode: int):
        self.data[:, self.head] = np.asarray(values, dtype=np.float32)
        self.valid[self.head] = bool(valid)
        self.seq[self.head] = np.uint32(sequence)
        self.mode[self.head] = np.uint8(mode)
        self.head = (self.head + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

    def append_batch(
        self,
        values: np.ndarray,
        valid: np.ndarray,
        sequence: np.ndarray,
        mode: np.ndarray,
    ):
        values = np.asarray(values, dtype=np.float32)
        if values.ndim != 2 or values.shape[0] != self.channels:
            raise ValueError("values must have shape (channels, samples)")
        n = values.shape[1]
        if n <= 0:
            return
        original_n = int(n)
        valid = np.asarray(valid, dtype=bool)
        sequence = np.asarray(sequence, dtype=np.uint32)
        mode = np.asarray(mode, dtype=np.uint8)
        if not (valid.size == sequence.size == mode.size == n):
            raise ValueError("batch metadata length mismatch")
        if n > self.capacity:
            values = values[:, -self.capacity:]
            valid = valid[-self.capacity:]
            sequence = sequence[-self.capacity:]
            mode = mode[-self.capacity:]
            n = self.capacity

        first = min(n, self.capacity - self.head)
        end = self.head + first
        self.data[:, self.head:end] = values[:, :first]
        self.valid[self.head:end] = valid[:first]
        self.seq[self.head:end] = sequence[:first]
        self.mode[self.head:end] = mode[:first]
        remaining = n - first
        if remaining:
            self.data[:, :remaining] = values[:, first:]
            self.valid[:remaining] = valid[first:]
            self.seq[:remaining] = sequence[first:]
            self.mode[:remaining] = mode[first:]
        self.head = (self.head + n) % self.capacity
        self.count = min(self.count + n, self.capacity)
        self.total_appended += original_n

    def latest(self, n: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        n = int(min(max(n, 0), self.count))
        if n == 0:
            return (
                np.zeros((self.channels, 0), dtype=np.float32),
                np.zeros(0, dtype=bool),
                np.zeros(0, dtype=np.uint32),
                np.zeros(0, dtype=np.uint8),
            )
        start = (self.head - n) % self.capacity
        if start < self.head:
            idx = slice(start, self.head)
            return self.data[:, idx].copy(), self.valid[idx].copy(), self.seq[idx].copy(), self.mode[idx].copy()
        idxs = np.r_[np.arange(start, self.capacity), np.arange(0, self.head)]
        return self.data[:, idxs].copy(), self.valid[idxs].copy(), self.seq[idxs].copy(), self.mode[idxs].copy()


class ClockAxisItem(pg.AxisItem):
    """EEG paper time axis formatted as HH:MM:SS."""

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            seconds = max(0, int(round(value)))
            hours, rem = divmod(seconds, 3600)
            minutes, secs = divmod(rem, 60)
            labels.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
        return labels


class PsdWorkerSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class PsdWorker(QtCore.QRunnable):
    """Run the relatively expensive PSD/quality calculation off the GUI thread."""

    def __init__(
        self, owner, request_id: int, x: np.ndarray, valid: np.ndarray,
        seq: np.ndarray, mode: np.ndarray, sos_band: np.ndarray,
        use_notch: bool, live_fast: bool = False,
    ):
        super().__init__()
        self.owner = owner
        self.request_id = request_id
        self.x = np.asarray(x, dtype=float)
        self.valid = np.asarray(valid, dtype=bool)
        self.seq = np.asarray(seq, dtype=np.uint32)
        self.mode = np.asarray(mode, dtype=np.uint8)
        self.sos_band = np.asarray(sos_band, dtype=float).copy()
        self.use_notch = bool(use_notch)
        self.live_fast = bool(live_fast)
        self.signals = PsdWorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            if self.live_fast:
                result = self.owner.compute_live_psd_fast(
                    self.x, self.valid, self.seq, self.mode,
                    sos_band=self.sos_band, use_notch=self.use_notch,
                )
            else:
                result = self.owner.compute_alpha_from_window(
                    self.x, self.valid, self.seq, self.mode,
                    sos_band=self.sos_band, use_notch=self.use_notch,
                )
            self.signals.finished.emit(self.request_id, (result, self.x, self.valid))
        except Exception as exc:  # pragma: no cover - surfaced in the GUI
            self.signals.failed.emit(self.request_id, str(exc))


@dataclass
class FilteredBatch:
    generation: int
    filtered: np.ndarray
    valid: np.ndarray
    sequence: np.ndarray
    modes: np.ndarray


class LiveFilterWorker:
    """Own the continuous live IIR state outside the Qt GUI thread.

    Transport reception and raw BIN writing must never wait for filtering or
    painting. Input samples are processed in order; display results use a
    bounded ring-like queue so a hidden or blocked window cannot grow memory
    forever. The raw BIN path remains independent and lossless while healthy.
    """

    _STOP = object()

    def __init__(self, sos_band: np.ndarray, sos_notch: np.ndarray, use_notch: bool):
        self._in = queue.Queue()
        self._out = queue.Queue(maxsize=FILTER_OUTPUT_MAX_BATCHES)
        self._thread = threading.Thread(
            target=self._run, name="OmniBCI-LiveFilter", daemon=True
        )
        self._lock = threading.Lock()
        self._running = False
        self._queued_samples = 0
        self._output_samples = 0
        self.peak_queued_samples = 0
        self.batches_processed = 0
        self.errors = 0
        self.display_dropped_samples = 0
        self.last_error = ""
        self._generation = 0
        self._sos_band = np.asarray(sos_band, dtype=float).copy()
        self._sos_notch = np.asarray(sos_notch, dtype=float).copy()
        self._use_notch = bool(use_notch)
        self._reset_state()

    def start(self):
        if self._thread.is_alive():
            return
        self._running = True
        self._thread.start()

    def configure(self, generation: int, sos_band: np.ndarray, sos_notch: np.ndarray, use_notch: bool):
        self._in.put((
            "config", int(generation), np.asarray(sos_band, dtype=float).copy(),
            np.asarray(sos_notch, dtype=float).copy(), bool(use_notch),
        ))

    def submit(self, generation: int, values, valid, sequence, modes):
        values = np.asarray(values, dtype=np.float32).copy()
        valid = np.asarray(valid, dtype=bool).copy()
        sequence = np.asarray(sequence, dtype=np.uint32).copy()
        modes = np.asarray(modes, dtype=np.uint8).copy()
        n = int(values.shape[1]) if values.ndim == 2 else 0
        if n <= 0:
            return
        with self._lock:
            self._queued_samples += n
            self.peak_queued_samples = max(self.peak_queued_samples, self._queued_samples)
        self._in.put(("batch", int(generation), values, valid, sequence, modes))

    def drain(self, max_batches: int = 16):
        out = []
        for _ in range(max(1, int(max_batches))):
            try:
                batch = self._out.get_nowait()
            except queue.Empty:
                break
            out.append(batch)
            with self._lock:
                self._output_samples = max(0, self._output_samples - int(batch.filtered.shape[1]))
        return out

    def metrics(self):
        with self._lock:
            return {
                "queued_samples": int(self._queued_samples),
                "output_samples": int(self._output_samples),
                "peak_queued_samples": int(self.peak_queued_samples),
                "batches_processed": int(self.batches_processed),
                "errors": int(self.errors),
                "display_dropped_samples": int(self.display_dropped_samples),
                "last_error": str(self.last_error),
            }

    def shutdown(self, timeout: float = 3.0):
        if not self._thread.is_alive():
            return
        self._in.put(self._STOP)
        self._thread.join(max(0.1, float(timeout)))
        self._running = False

    def _reset_state(self):
        self._zi_band = np.zeros((CHANNELS, self._sos_band.shape[0], 2), dtype=float)
        self._zi_notch = np.zeros((CHANNELS, self._sos_notch.shape[0], 2), dtype=float)
        self._last_input = np.zeros(CHANNELS, dtype=float)
        self._have_input = np.zeros(CHANNELS, dtype=bool)

    def _filter(self, values: np.ndarray, valid: np.ndarray) -> np.ndarray:
        source = np.asarray(values, dtype=float)
        # ``source`` may contain per-channel NaNs inserted for ADC saturation.
        # Hold those samples only to evolve the IIR; keep a channel-specific
        # mask so they remain display gaps instead of invented EEG.
        channel_good_matrix = np.asarray(valid, dtype=bool)[None, :] & np.isfinite(source)
        filled = source.copy()
        for ch in range(CHANNELS):
            channel_good = channel_good_matrix[ch]
            if not self._have_input[ch]:
                first_candidates = np.flatnonzero(channel_good)
                if first_candidates.size:
                    first_idx = int(first_candidates[0])
                    first_value = float(filled[ch, first_idx])
                    filled[ch, :first_idx] = first_value
                    self._zi_band[ch] = signal.sosfilt_zi(self._sos_band) * first_value
                    self._zi_notch[ch].fill(0.0)
                    self._last_input[ch] = first_value
                    self._have_input[ch] = True
            n_samples = int(filled.shape[1])
            if not n_samples:
                continue
            if channel_good.all():
                self._last_input[ch] = float(filled[ch, -1])
                self._have_input[ch] = True
                continue
            original = filled[ch].copy()
            seed = float(self._last_input[ch]) if self._have_input[ch] else 0.0
            last_good_index = np.where(channel_good, np.arange(n_samples, dtype=np.int64), -1)
            np.maximum.accumulate(last_good_index, out=last_good_index)
            has_previous = last_good_index >= 0
            filled[ch, ~has_previous] = seed
            if np.any(has_previous):
                filled[ch, has_previous] = original[last_good_index[has_previous]]
            good_indices = np.flatnonzero(channel_good)
            if good_indices.size:
                self._last_input[ch] = float(original[int(good_indices[-1])])
                self._have_input[ch] = True

        band_zi = np.transpose(self._zi_band, (1, 0, 2))
        filtered, band_zf = signal.sosfilt(self._sos_band, filled, axis=1, zi=band_zi)
        self._zi_band = np.transpose(band_zf, (1, 0, 2))
        if self._use_notch:
            notch_zi = np.transpose(self._zi_notch, (1, 0, 2))
            filtered, notch_zf = signal.sosfilt(
                self._sos_notch, filtered, axis=1, zi=notch_zi
            )
            self._zi_notch = np.transpose(notch_zf, (1, 0, 2))

        bad_channels = np.flatnonzero(
            ~np.all(np.isfinite(filtered), axis=1)
            | ~np.all(np.isfinite(self._zi_band), axis=(1, 2))
            | ~np.all(np.isfinite(self._zi_notch), axis=(1, 2))
        )
        for ch in bad_channels:
            seed = float(self._last_input[ch]) if np.isfinite(self._last_input[ch]) else 0.0
            self._zi_band[ch] = signal.sosfilt_zi(self._sos_band) * seed
            self._zi_notch[ch].fill(0.0)
            filtered[ch] = np.nan_to_num(filtered[ch], nan=seed, posinf=seed, neginf=seed)

        # NaN gaps are much cheaper for pyqtgraph than thousands of alternating
        # full-scale vertical segments, and they isolate only the bad channel.
        filtered[~channel_good_matrix] = np.nan
        return np.asarray(filtered, dtype=np.float32)

    def _run(self):
        while True:
            item = self._in.get()
            if item is self._STOP:
                return
            kind = item[0]
            if kind == "config":
                _, generation, band, notch, use_notch = item
                self._generation = int(generation)
                self._sos_band = band
                self._sos_notch = notch
                self._use_notch = bool(use_notch)
                self._reset_state()
                continue
            _, generation, values, valid, sequence, modes = item
            n = int(values.shape[1])
            try:
                if int(generation) != self._generation:
                    continue
                filtered = self._filter(values, valid)
                batch = FilteredBatch(
                    int(generation), filtered, valid, sequence, modes
                )
                try:
                    self._out.put_nowait(batch)
                except queue.Full:
                    # Display results are bounded like a ring buffer. Raw BIN
                    # bytes are already safe in the independent writer, so an
                    # inactive/blocked Qt window must not grow memory forever.
                    try:
                        dropped = self._out.get_nowait()
                        dropped_n = int(dropped.filtered.shape[1])
                    except queue.Empty:
                        dropped_n = 0
                    with self._lock:
                        self._output_samples = max(0, self._output_samples - dropped_n)
                        self.display_dropped_samples += dropped_n
                    self._out.put_nowait(batch)
                with self._lock:
                    self._output_samples += n
                    self.batches_processed += 1
            except Exception as exc:
                with self._lock:
                    self.errors += 1
                    self.last_error = str(exc)
                self._reset_state()
            finally:
                with self._lock:
                    self._queued_samples = max(0, self._queued_samples - n)


def expand_compact_ble_payload(payload: bytes, frame_count: int) -> bytes:
    """Expand BLE V2 compact records back to the standard 48-byte BIN format."""
    if len(payload) != int(frame_count) * BLE_COMPACT_FRAME_BYTES:
        raise ValueError("BLE compact payload length mismatch")
    out = bytearray()
    for index in range(int(frame_count)):
        record = payload[
            index * BLE_COMPACT_FRAME_BYTES:(index + 1) * BLE_COMPACT_FRAME_BYTES
        ]
        sequence = record[0:4]
        timestamp = record[4:8]
        ads_raw = record[8:35]
        flags = record[35]
        frame = bytearray(FRAME_BYTES)
        frame[0:4] = bytes((SYNC1, SYNC2, 1, 1))
        frame[4:8] = sequence
        frame[8:12] = timestamp
        frame[12:15] = ads_raw[0:3]
        frame[15] = flags
        frame[16:40] = ads_raw[3:27]
        frame[40:42] = b"\x00\x00"
        frame[42] = 2 if (flags & 0x04) else 0
        if flags & 0x08:
            mode = 4
        elif flags & 0x10:
            mode = 3
        elif flags & 0x40:
            mode = 0
        elif flags & 0x20:
            mode = 1
        else:
            mode = 2
        frame[43] = mode
        frame[44] = 0
        frame[45] = 0
        struct.pack_into("<H", frame, 46, crc16_ccitt(frame[:46]))
        out.extend(frame)
    return bytes(out)


class SerialTransportWorker:
    """Continuously drain pyserial outside the Qt event loop.

    The worker owns *reads* and an in-memory byte deque.  GUI/command writes may
    still use the same full-duplex serial handle.  reset_input_buffer() is
    serialized with reads so configuration ACKs and EEG bytes cannot race an OS
    buffer reset.  No Qt signal is emitted per read.
    """

    def __init__(self, ser_handle):
        self.ser = ser_handle
        self._chunks = deque()
        self._data_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="OmniBCI-SerialReader", daemon=True
        )
        self._queued_bytes = 0
        self._peak_queued_bytes = 0
        self._read_calls = 0
        self._read_errors = 0
        self._overflow_events = 0
        self._last_gap_s = 0.0
        self._max_gap_s = 0.0
        self._last_rx_monotonic = None
        self.buffer_configured = False
        self.buffer_error = ""

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.ser is None or not self.ser.is_open:
                    break
                with self._read_lock:
                    waiting = int(self.ser.in_waiting)
                    want = min(
                        SERIAL_READER_MAX_READ_BYTES,
                        max(1, waiting),
                    )
                    payload = self.ser.read(want)
                if not payload:
                    continue
                now = time.monotonic()
                if self._last_rx_monotonic is not None:
                    gap = max(0.0, now - self._last_rx_monotonic)
                    self._last_gap_s = gap
                    self._max_gap_s = max(self._max_gap_s, gap)
                self._last_rx_monotonic = now
                with self._data_lock:
                    self._chunks.append(bytes(payload))
                    self._queued_bytes += len(payload)
                    self._peak_queued_bytes = max(
                        self._peak_queued_bytes, self._queued_bytes
                    )
                    if self._queued_bytes > SERIAL_HOST_MAX_QUEUE_BYTES:
                        # Do not silently discard EEG.  Count pressure and keep
                        # the queue lossless; diagnostics will expose the host
                        # processing stall while RAM acts as the absorber.
                        self._overflow_events += 1
                self._read_calls += 1
            except Exception:
                self._read_errors += 1
                if self._stop.wait(0.02):
                    break

    def queued_data_bytes(self) -> int:
        with self._data_lock:
            return int(self._queued_bytes)

    def drain_data(self, max_bytes: int = SERIAL_MAX_PROCESS_BYTES) -> bytes:
        max_bytes = max(1, int(max_bytes))
        parts = []
        total = 0
        with self._data_lock:
            while self._chunks and total < max_bytes:
                chunk = self._chunks[0]
                remaining = max_bytes - total
                if len(chunk) <= remaining:
                    parts.append(self._chunks.popleft())
                    total += len(chunk)
                else:
                    parts.append(chunk[:remaining])
                    self._chunks[0] = chunk[remaining:]
                    total += remaining
                    break
            self._queued_bytes = max(0, self._queued_bytes - total)
        return b"".join(parts)

    def clear_data(self, clear_driver: bool = True):
        with self._data_lock:
            self._chunks.clear()
            self._queued_bytes = 0
        if clear_driver and self.ser is not None and self.ser.is_open:
            try:
                with self._read_lock:
                    self.ser.reset_input_buffer()
            except Exception:
                pass

    def metrics(self) -> dict:
        with self._data_lock:
            queued = int(self._queued_bytes)
            peak = int(self._peak_queued_bytes)
        return {
            "queued_bytes": queued,
            "peak_queued_bytes": peak,
            "read_calls": int(self._read_calls),
            "read_errors": int(self._read_errors),
            "overflow_events": int(self._overflow_events),
            "last_gap_s": float(self._last_gap_s),
            "max_gap_s": float(self._max_gap_s),
            "buffer_configured": bool(self.buffer_configured),
            "buffer_error": str(self.buffer_error),
        }

    def stop(self, timeout: float = 2.0, close_port: bool = False):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.1, float(timeout)))
        if close_port and self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass


class AsyncRawWriter:
    """Lossless background writer with exact one-minute BIN rotation.

    Transport/Qt only enqueue bytes.  File open/write/flush/rotation and JSON
    metadata all happen in this worker, so restoring segmented BIN recording
    does not put disk latency back on the UI thread.
    """

    _STOP = object()

    def __init__(self):
        self._queue = queue.Queue(maxsize=RAW_WRITER_QUEUE_CHUNKS)
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._error: Optional[str] = None
        self._queued_bytes = 0
        self.peak_queued_bytes = 0
        self.bytes_written = 0
        self.dropped_bytes = 0
        self._folder = ""
        self._session_id = ""
        self._session_prefix = ""
        self._session_started_at = ""
        self._manifest_path = ""
        self._first_path = ""
        self._current_path = ""
        self._segment_index = 0
        self._segment_bytes = 0
        self._segments = []
        self._configuration = {}

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return int(self._queued_bytes)

    @property
    def current_path(self) -> str:
        with self._lock:
            return str(self._current_path)

    @property
    def first_path(self) -> str:
        with self._lock:
            return str(self._first_path)

    @property
    def manifest_path(self) -> str:
        with self._lock:
            return str(self._manifest_path)

    @property
    def session_id(self) -> str:
        with self._lock:
            return str(self._session_id)

    @property
    def segment_count(self) -> int:
        with self._lock:
            return int(len(self._segments))

    @property
    def segment_index(self) -> int:
        with self._lock:
            return int(self._segment_index)

    @property
    def segment_bytes(self) -> int:
        with self._lock:
            return int(self._segment_bytes)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "session_id": self._session_id,
                "session_prefix": self._session_prefix,
                "manifest_path": self._manifest_path,
                "first_path": self._first_path,
                "current_path": self._current_path,
                "segment_index": int(self._segment_index),
                "segment_bytes": int(self._segment_bytes),
                "segment_count": int(len(self._segments)),
                "segments": [dict(item) for item in self._segments],
                "bytes_written": int(self.bytes_written),
                "queued_bytes": int(self._queued_bytes),
            }

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def start_session(self, folder: str, configuration: Optional[dict] = None):
        self.stop(timeout=2.0)
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        session_id = secrets.token_hex(3)
        prefix = f"{now:%m%d_%H%M}_{session_id}"
        first_path = target / f"{prefix}_minute01.bin"
        manifest = target / f"{prefix}_manifest.json"
        self._queue = queue.Queue(maxsize=RAW_WRITER_QUEUE_CHUNKS)
        self._started.clear()
        with self._lock:
            self._error = None
            self._queued_bytes = 0
            self.peak_queued_bytes = 0
            self.bytes_written = 0
            self.dropped_bytes = 0
            self._folder = str(target)
            self._session_id = session_id
            self._session_prefix = prefix
            self._session_started_at = now.isoformat(timespec="seconds")
            self._manifest_path = str(manifest)
            self._first_path = str(first_path)
            self._current_path = str(first_path)
            self._segment_index = 0
            self._segment_bytes = 0
            self._segments = []
            self._configuration = dict(configuration or {})
        self._thread = threading.Thread(
            target=self._run, name="OmniBCI-SegmentedRawWriter", daemon=True
        )
        self._thread.start()
        if not self._started.wait(2.0):
            raise RuntimeError("分包 BIN 写盘线程启动超时")
        if self.error:
            raise RuntimeError(self.error)

    def submit(self, data: bytes) -> bool:
        payload = bytes(data)
        if not payload:
            return True
        thread = self._thread
        if thread is None or not thread.is_alive() or self.error:
            return False
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self.dropped_bytes += len(payload)
                if self._error is None:
                    self._error = "原始 BIN 写盘队列已满；实时显示继续，但本次 BIN 已停止保证完整"
            return False
        with self._lock:
            self._queued_bytes += len(payload)
            self.peak_queued_bytes = max(self.peak_queued_bytes, self._queued_bytes)
        return True

    def stop(self, timeout: float = 10.0):
        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + max(0.5, float(timeout))
        while thread.is_alive():
            try:
                self._queue.put(self._STOP, timeout=0.05)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            with self._lock:
                if self._error is None:
                    self._error = "分包 BIN 写盘线程停止超时"
        else:
            self._thread = None

    def _segment_path(self, index: int) -> Path:
        return Path(self._folder) / f"{self._session_prefix}_minute{int(index):02d}.bin"

    @staticmethod
    def _sidecar_path(bin_path: Path) -> Path:
        return Path(bin_path).with_suffix(".meta.json")

    def _metadata_payload(self, status: str) -> dict:
        with self._lock:
            segments = [dict(item) for item in self._segments]
            return {
                "schema": RECORD_METADATA_SCHEMA,
                "status": status,
                "recording_id": self._session_id,
                "session_prefix": self._session_prefix,
                "session_started_at": self._session_started_at,
                "segment_target_seconds": RECORD_SEGMENT_SECONDS,
                "segment_target_bytes": RECORD_SEGMENT_BYTES,
                "total_bytes": int(self.bytes_written),
                "total_complete_frames": int(self.bytes_written // FRAME_BYTES),
                "configuration_at_session_start": dict(self._configuration),
                "segments": segments,
            }

    def _persist_metadata(self, status: str):
        payload = self._metadata_payload(status)
        manifest = self.manifest_path
        if manifest:
            self._write_json_atomic(Path(manifest), payload)
        with self._lock:
            segments = [dict(item) for item in self._segments]
            session_id = self._session_id
            prefix = self._session_prefix
            started = self._session_started_at
            manifest_name = Path(self._manifest_path).name if self._manifest_path else ""
            configuration = dict(self._configuration)
        for record in segments[-1:]:
            sidecar = self._sidecar_path(Path(record["path"]))
            self._write_json_atomic(
                sidecar,
                {
                    "schema": RECORD_METADATA_SCHEMA,
                    "status": record.get("status", status),
                    "recording_id": session_id,
                    "session_prefix": prefix,
                    "session_started_at": started,
                    "manifest_file": manifest_name,
                    **record,
                    "configuration": configuration,
                },
            )

    def _open_segment(self):
        with self._lock:
            self._segment_index += 1
            index = self._segment_index
            path = self._segment_path(index)
            self._current_path = str(path)
            if not self._first_path:
                self._first_path = str(path)
            self._segment_bytes = 0
            record = {
                "segment_index": int(index),
                "minute_label": f"minute{index:02d}",
                "path": str(path),
                "file": path.name,
                "sidecar_file": self._sidecar_path(path).name,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "bytes": 0,
                "complete_frames": 0,
                "duration_seconds": 0.0,
                "status": "recording",
            }
            self._segments.append(record)
        return open(path, "wb", buffering=RAW_WRITER_BUFFER_BYTES)

    def _update_current_record(self, status: str = "recording"):
        with self._lock:
            if not self._segments:
                return
            current = self._segments[-1]
            current["bytes"] = int(self._segment_bytes)
            current["complete_frames"] = int(self._segment_bytes // FRAME_BYTES)
            current["duration_seconds"] = round(
                self._segment_bytes / max(1, BYTES_PER_SECOND), 3
            )
            current["status"] = status
            if status != "recording":
                current["ended_at"] = datetime.now().isoformat(timespec="seconds")

    def _write_payload(self, handle, payload: bytes):
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            if handle is None:
                handle = self._open_segment()
            with self._lock:
                remaining = RECORD_SEGMENT_BYTES - self._segment_bytes
            take = min(len(view) - offset, remaining)
            written = handle.write(view[offset:offset + take])
            if written != take:
                raise OSError(f"BIN 写入不完整：请求 {take} 字节，实际 {written} 字节")
            offset += written
            with self._lock:
                self._segment_bytes += written
                self.bytes_written += written
                self._queued_bytes = max(0, self._queued_bytes - written)
                full = self._segment_bytes >= RECORD_SEGMENT_BYTES
            self._update_current_record("recording")
            if full:
                handle.flush()
                handle.close()
                handle = None
                self._update_current_record("complete")
                self._persist_metadata("recording")
        return handle

    def _run(self):
        handle = None
        status = "complete"
        try:
            handle = self._open_segment()
            self._persist_metadata("recording")
            self._started.set()
            last_flush = time.monotonic()
            last_metadata = last_flush
            while True:
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if item is self._STOP:
                    break
                if item is not None:
                    handle = self._write_payload(handle, item)
                now = time.monotonic()
                if handle is not None and now - last_flush >= RAW_WRITER_FLUSH_INTERVAL_S:
                    handle.flush()
                    last_flush = now
                if now - last_metadata >= RECORD_METADATA_UPDATE_INTERVAL_S:
                    self._update_current_record("recording")
                    self._persist_metadata("recording")
                    last_metadata = now
            if handle is not None:
                handle.flush()
                handle.close()
                handle = None
            self._update_current_record("complete")
        except Exception as exc:
            status = "error"
            with self._lock:
                self._error = f"原始 BIN 写盘失败：{exc}"
            self._update_current_record("error")
        finally:
            self._started.set()
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            try:
                self._persist_metadata(status)
            except Exception as meta_exc:
                with self._lock:
                    if self._error is None:
                        self._error = f"BIN 元数据写入失败：{meta_exc}"

class BleTransportWorker(QtCore.QThread):
    """Own a Bleak asyncio loop outside the Qt GUI thread.

    DATA notifications are forwarded as opaque byte chunks. STATUS ACK packets
    are also placed into a thread-safe queue so synchronous configuration
    dialogs can wait for hardware readback without blocking the BLE event loop.
    """

    scan_started = QtCore.Signal()
    scan_finished = QtCore.Signal(object)
    connecting = QtCore.Signal(str)
    connected = QtCore.Signal(str, str, int, bool)
    disconnected = QtCore.Signal(str, bool)
    data_received = QtCore.Signal(object)
    status_received = QtCore.Signal(object)
    info = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        self._response_waiters = {}
        self._next_request_id = 1
        self.device_info = None
        self.config_snapshot = None
        # DATA notifications stay in the BLE worker thread.  A lock-protected
        # deque is drained by the GUI timer, avoiding one queued Qt event per
        # notification (a common cause of visible Windows GUI stalls).
        self._data_chunks = deque()
        self._data_lock = threading.Lock()
        self._queued_data_bytes = 0
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._devices = {}
        self._desired_key: Optional[str] = None
        self._manual_disconnect = False
        self._reconnect_task = None
        self._status_poll_task = None
        self._reliable_watchdog_task = None
        self._connect_lock = None
        self._closing = False
        self._streaming_hint = False
        self._streaming_hint_started_monotonic = 0.0
        self._timing_lock = threading.Lock()
        self._last_notify_monotonic: Optional[float] = None
        self._notify_gap_last_s = 0.0
        self._notify_gap_max_s = 0.0
        self._notify_burst_max_bytes = 0
        self._notify_gap_over_100ms = 0
        self._notify_gap_events = deque(maxlen=32)
        self._notify_gap_samples = deque(maxlen=BLE_ADAPTIVE_GAP_SAMPLES)
        self._notify_gap_ewma_s = 0.0
        self._adaptive_profile = "learning"

        # Bleak's DATA notification callback must do almost no Python work.
        # It only timestamps + copies bytes into this lossless host queue, then
        # returns to the Windows BLE stack immediately. Reliable decoding, CRC,
        # compact-frame expansion and ACK/NACK decisions run in a separate
        # decoder thread, so a busy Qt paint/PSD turn cannot block the notify
        # callback long enough to fill the MCU retention ring.
        self._notify_decode_queue = queue.Queue()
        self._notify_decode_stop = threading.Event()
        self._notify_decode_sentinel = object()
        self._notify_decode_lock = threading.Lock()
        self._notify_decode_queued_bytes = 0
        self._notify_decode_peak_bytes = 0
        self._notify_decode_errors = 0
        self._notify_decoder_thread: Optional[threading.Thread] = None

        # Reliable BLE block reassembly/ACK state. DATA is decoded by the
        # dedicated decoder thread; the GUI only ever receives ordered standard
        # 48-byte ADS frames.
        self._reliable_lock = threading.Lock()
        self._reliable_rx_buf = bytearray()
        self._reliable_session_id = None
        self._reliable_accept_any_session = True
        self._reliable_expected_block = 0
        self._reliable_pending = {}
        self._reliable_last_ack_sent = 0xFFFFFFFF
        self._reliable_last_ack_time = 0.0
        self._reliable_last_nack = None
        self._reliable_blocks_received = 0
        self._reliable_blocks_delivered = 0
        self._reliable_block_crc_bad = 0
        self._reliable_sync_drop = 0
        self._reliable_duplicates = 0
        self._reliable_out_of_order = 0
        self._reliable_retransmitted_received = 0
        self._reliable_gap_markers = 0
        self._reliable_ack_sent = 0
        self._reliable_nack_sent = 0
        self._reliable_control_errors = 0
        # V18 hotfix: ACK/NACK writes are generated off the decoder thread and
        # can become obsolete before Windows actually transmits them. Suppress
        # those stale controls instead of letting an already-repaired NACK hit
        # the MCU after the corresponding cumulative ACK has released the block.
        self._reliable_stale_nack_suppressed = 0
        self._reliable_stale_ack_suppressed = 0
        self._reliable_last_ack_wire = 0xFFFFFFFF
        self._reliable_max_pending = 0
        self._reliable_gap_sequence = None
        self._reliable_gap_first_seen = 0.0
        self._reliable_watchdog_nacks = 0
        self._reliable_forced_skips = 0
        self._reliable_last_delivery_monotonic = 0.0
        self._watchdog_reconnects = 0
        self._watchdog_last_reconnect_monotonic = 0.0
        self._gatt_write_lock = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._connect_lock = asyncio.Lock()
        self._gatt_write_lock = asyncio.Lock()
        self._notify_decode_stop.clear()
        self._notify_decoder_thread = threading.Thread(
            target=self._notify_decoder_loop,
            name="OmniBCI-BLEDecoder",
            daemon=True,
        )
        self._notify_decoder_thread.start()
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._notify_decode_stop.set()
            try:
                self._notify_decode_queue.put_nowait(self._notify_decode_sentinel)
            except Exception:
                pass
            decoder = self._notify_decoder_thread
            if decoder is not None and decoder.is_alive():
                decoder.join(timeout=2.0)
            pending = list(asyncio.all_tasks(self._loop))
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    def _submit(self, coroutine):
        if not BLE_AVAILABLE:
            raise RuntimeError(f"未安装 Bleak：{BLE_IMPORT_ERROR}")
        if not self._ready.wait(3.0) or self._loop is None:
            raise RuntimeError("BLE 后台线程未就绪")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def scan(self, timeout: float = 5.0):
        try:
            self._submit(self._scan(float(timeout)))
        except Exception as exc:
            self.error.emit(str(exc))

    async def _scan(self, timeout: float):
        self.scan_started.emit()
        try:
            devices = await BleakScanner.discover(timeout=max(1.0, timeout))
            rows = []
            self._devices = {}
            for device in devices:
                address = str(getattr(device, "address", "") or "")
                if not address:
                    continue
                name = str(getattr(device, "name", "") or "").strip() or "未命名 BLE 设备"
                self._devices[address] = device
                rows.append({
                    "key": address,
                    "name": name,
                    "address": address,
                    "preferred": name in BLE_DEVICE_NAMES,
                })
            rows.sort(key=lambda item: (not item["preferred"], item["name"].lower(), item["address"]))
            self.scan_finished.emit(rows)
        except Exception as exc:
            self.error.emit(f"BLE 扫描失败：{exc}")
            self.scan_finished.emit([])

    def connect_device(self, key: str):
        self._desired_key = str(key)
        self._manual_disconnect = False
        try:
            future = self._submit(self._connect_to_device(str(key), reconnected=False))
            future.add_done_callback(self._initial_connect_done)
        except Exception as exc:
            self.error.emit(str(exc))

    def _initial_connect_done(self, future):
        try:
            future.result()
        except Exception as exc:
            self.error.emit(f"BLE 连接失败：{exc}")

    async def _resolve_device(self, key: str):
        device = self._devices.get(key)
        if device is not None:
            return device
        finder = getattr(BleakScanner, "find_device_by_address", None)
        if finder is None:
            return None
        return await finder(key, timeout=10.0)

    async def _connect_to_device(self, key: str, reconnected: bool):
        if self._closing:
            return
        async with self._connect_lock:
            if self._desired_key != key:
                return
            if self._client is not None and bool(getattr(self._client, "is_connected", False)):
                return
            if not reconnected:
                self.connecting.emit(key)
            device = await self._resolve_device(key)
            if self._desired_key != key:
                return
            if device is None:
                raise RuntimeError("找不到所选 BLE 设备，请重新扫描。")

            client = BleakClient(
                device,
                disconnected_callback=self._on_disconnected,
                timeout=15.0,
            )
            try:
                await client.connect()
                if self._desired_key != key:
                    await client.disconnect()
                    return
                services = client.services
                missing = [
                    uuid for uuid in (
                        BLE_DATA_UUID, BLE_CONTROL_UUID, BLE_STATUS_UUID, BLE_RESPONSE_UUID
                    )
                    if services.get_characteristic(uuid) is None
                ]
                if missing:
                    raise RuntimeError("设备缺少 OmniBCI BLE 特征，可能选错设备或固件版本不匹配。")
                self._client = client
                if not reconnected:
                    self.reset_reliable_state(reset_metrics=True)
                else:
                    with self._reliable_lock:
                        self._reliable_accept_any_session = True
                await client.start_notify(BLE_DATA_UUID, self._on_data)
                await client.start_notify(BLE_STATUS_UUID, self._on_status)
                await client.start_notify(BLE_RESPONSE_UUID, self._on_response)
                # Give the resubscribed link a fresh stall deadline. Otherwise
                # the watchdog can immediately disconnect again based on the
                # timestamp from before the radio interruption.
                self._last_notify_monotonic = time.monotonic()
                hello = await self._request(MSG_HELLO, timeout=2.5)
                if len(hello) < 11 or hello[0] != 0:
                    raise RuntimeError("固件握手响应无效")
                firmware_version = hello[1]
                protocol_version = hello[4]
                if firmware_version != BLE_FIRMWARE_VERSION:
                    raise RuntimeError(
                        f"固件版本不兼容：需要 V{BLE_FIRMWARE_VERSION}，设备为 V{firmware_version}"
                    )
                if protocol_version != BLE_DEVICE_PROTOCOL_VERSION:
                    raise RuntimeError(
                        f"通信协议不兼容：需要 V{BLE_DEVICE_PROTOCOL_VERSION}，设备为 V{protocol_version}"
                    )
                self.device_info = {
                    "firmware": (hello[1], hello[2], hello[3]),
                    "protocol": protocol_version,
                    "capabilities": int.from_bytes(hello[5:7], "little"),
                    "boot_id": int.from_bytes(hello[7:11], "little"),
                }
                if not (reconnected and self._streaming_hint):
                    self.config_snapshot = decode_config_snapshot(
                        await self._request(MSG_GET_CONFIG, timeout=3.0)
                    )
                status = bytes(await client.read_gatt_char(BLE_STATUS_UUID))
                self._publish_status(status)
                mtu = int(getattr(client, "mtu_size", 23) or 23)
                name = str(getattr(device, "name", "") or BLE_DEVICE_NAME)
                address = str(getattr(device, "address", key) or key)
                self._manual_disconnect = False
                self.connected.emit(name, address, mtu, bool(reconnected))
                if self._status_poll_task is not None:
                    self._status_poll_task.cancel()
                self._status_poll_task = asyncio.create_task(self._status_poll_loop(client))
                if self._reliable_watchdog_task is not None:
                    self._reliable_watchdog_task.cancel()
                self._reliable_watchdog_task = asyncio.create_task(
                    self._reliable_watchdog_loop(client)
                )
            except Exception:
                if self._client is client:
                    self._client = None
                try:
                    if bool(getattr(client, "is_connected", False)):
                        await client.disconnect()
                except Exception:
                    pass
                raise

    @staticmethod
    def _make_reliable_control_packet(
        command_type: int, session_id: int, seq_a: int, seq_b: int = 0
    ) -> bytes:
        body = (
            BLE_CTRL_MAGIC
            + bytes((BLE_CTRL_VERSION, int(command_type) & 0xFF))
            + struct.pack(
                "<III",
                int(session_id) & 0xFFFFFFFF,
                int(seq_a) & 0xFFFFFFFF,
                int(seq_b) & 0xFFFFFFFF,
            )
        )
        return body + struct.pack("<H", crc16_ccitt(body))

    async def _send_reliable_control(self, packet: bytes, kind: str):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            return
        try:
            lock = self._gatt_write_lock
            if lock is None:
                return
            async with lock:
                # Revalidate immediately before the GATT write. A missing block
                # can be repaired while an older NACK is waiting behind another
                # Windows GATT operation. Sending that obsolete NACK after the
                # cumulative ACK makes the firmware report an "unknown NACK"
                # even though no data was actually lost.
                packet_to_send = bytes(packet)
                if (
                    kind in ("ack", "nack")
                    and len(packet_to_send) == BLE_CTRL_PACKET_BYTES
                    and packet_to_send[:2] == BLE_CTRL_MAGIC
                    and packet_to_send[2] == BLE_CTRL_VERSION
                ):
                    session_id, seq_a, seq_b = struct.unpack_from("<III", packet_to_send, 4)
                    with self._reliable_lock:
                        current_session = self._reliable_session_id
                        if current_session is None or int(session_id) != int(current_session):
                            if kind == "nack":
                                self._reliable_stale_nack_suppressed += 1
                            else:
                                self._reliable_stale_ack_suppressed += 1
                            return

                        if kind == "nack":
                            expected = int(self._reliable_expected_block)
                            pending_keys = sorted(self._reliable_pending)
                            has_hole = bool(pending_keys and pending_keys[0] > expected)
                            # If the decoder has already advanced past this range
                            # (or the pending hole disappeared), the repair request
                            # is stale and must not reach the C3.
                            if not has_hole or expected > int(seq_b):
                                self._reliable_stale_nack_suppressed += 1
                                return
                            if expected != int(seq_a):
                                seq_a = expected
                                seq_b = max(expected, int(seq_b))
                                packet_to_send = self._make_reliable_control_packet(
                                    BLE_CTRL_NACK_RANGE, current_session, seq_a, seq_b
                                )
                        else:
                            ack_seq = int(seq_a)
                            last_wire = int(self._reliable_last_ack_wire)
                            if last_wire != 0xFFFFFFFF and ack_seq <= last_wire:
                                self._reliable_stale_ack_suppressed += 1
                                return

                # ACK and NACK are tiny CRC-protected, idempotent/repeated
                # controls. Write-without-response keeps them out of the Windows
                # response queue and prevents a repair request from blocking
                # later cumulative ACKs. RESET remains write-with-response.
                await client.write_gatt_char(
                    BLE_CONTROL_UUID, packet_to_send,
                    response=(kind not in ("ack", "nack"))
                )
            with self._reliable_lock:
                if kind == "ack":
                    self._reliable_ack_sent += 1
                    if len(packet_to_send) >= 12:
                        self._reliable_last_ack_wire = struct.unpack_from("<I", packet_to_send, 8)[0]
                elif kind == "nack":
                    self._reliable_nack_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            with self._reliable_lock:
                self._reliable_control_errors += 1

    def _schedule_reliable_control(self, packet: bytes, kind: str):
        """Schedule a GATT control write from either asyncio or decoder thread."""
        loop = self._loop
        if loop is None or self._closing:
            return
        payload = bytes(packet)
        control_kind = str(kind)

        def _spawn():
            if self._closing:
                return
            try:
                asyncio.create_task(self._send_reliable_control(payload, control_kind))
            except RuntimeError:
                pass

        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            pass

    def _enqueue_notify_for_decode(self, payload: bytes):
        payload = bytes(payload)
        if not payload:
            return
        with self._notify_decode_lock:
            self._notify_decode_queued_bytes += len(payload)
            self._notify_decode_peak_bytes = max(
                self._notify_decode_peak_bytes, self._notify_decode_queued_bytes
            )
        self._notify_decode_queue.put(payload)

    def _clear_notify_decode_queue(self):
        removed = 0
        while True:
            try:
                item = self._notify_decode_queue.get_nowait()
            except queue.Empty:
                break
            if item is self._notify_decode_sentinel:
                continue
            try:
                removed += len(item)
            except Exception:
                pass
        with self._notify_decode_lock:
            self._notify_decode_queued_bytes = max(
                0, self._notify_decode_queued_bytes - removed
            )

    def _notify_decoder_loop(self):
        while not self._notify_decode_stop.is_set():
            try:
                item = self._notify_decode_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is self._notify_decode_sentinel:
                return
            payload = bytes(item)
            with self._notify_decode_lock:
                self._notify_decode_queued_bytes = max(
                    0, self._notify_decode_queued_bytes - len(payload)
                )
            try:
                with self._reliable_lock:
                    ordered_payloads, control_packets = self._decode_reliable_bytes_locked(payload)
                if ordered_payloads:
                    joined = b"".join(ordered_payloads)
                    with self._data_lock:
                        self._data_chunks.append(joined)
                        self._queued_data_bytes += len(joined)
                for packet, kind in control_packets:
                    self._schedule_reliable_control(packet, kind)
            except Exception:
                with self._notify_decode_lock:
                    self._notify_decode_errors += 1

    def reset_reliable_state(self, reset_metrics: bool = True):
        with self._reliable_lock:
            self._reliable_rx_buf.clear()
            self._reliable_session_id = None
            self._reliable_accept_any_session = True
            self._reliable_expected_block = 0
            self._reliable_pending.clear()
            self._reliable_last_ack_sent = 0xFFFFFFFF
            self._reliable_last_ack_time = 0.0
            self._reliable_last_nack = None
            self._reliable_last_ack_wire = 0xFFFFFFFF
            self._reliable_gap_sequence = None
            self._reliable_gap_first_seen = 0.0
            self._reliable_last_delivery_monotonic = time.monotonic()
            if reset_metrics:
                self._reliable_blocks_received = 0
                self._reliable_blocks_delivered = 0
                self._reliable_block_crc_bad = 0
                self._reliable_sync_drop = 0
                self._reliable_duplicates = 0
                self._reliable_out_of_order = 0
                self._reliable_retransmitted_received = 0
                self._reliable_gap_markers = 0
                self._reliable_ack_sent = 0
                self._reliable_nack_sent = 0
                self._reliable_control_errors = 0
                self._reliable_stale_nack_suppressed = 0
                self._reliable_stale_ack_suppressed = 0
                self._reliable_max_pending = 0
                self._reliable_watchdog_nacks = 0
                self._reliable_forced_skips = 0
                self._watchdog_reconnects = 0

    def reliable_metrics(self):
        with self._reliable_lock:
            return {
                "blocks_received": int(self._reliable_blocks_received),
                "blocks_delivered": int(self._reliable_blocks_delivered),
                "block_crc_bad": int(self._reliable_block_crc_bad),
                "sync_drop": int(self._reliable_sync_drop),
                "duplicates": int(self._reliable_duplicates),
                "out_of_order": int(self._reliable_out_of_order),
                "retransmitted_received": int(self._reliable_retransmitted_received),
                "gap_markers": int(self._reliable_gap_markers),
                "ack_sent": int(self._reliable_ack_sent),
                "nack_sent": int(self._reliable_nack_sent),
                "control_errors": int(self._reliable_control_errors),
                "stale_nack_suppressed": int(self._reliable_stale_nack_suppressed),
                "stale_ack_suppressed": int(self._reliable_stale_ack_suppressed),
                "pending_blocks": int(len(self._reliable_pending)),
                "max_pending": int(self._reliable_max_pending),
                "expected_block": int(self._reliable_expected_block),
                "watchdog_nacks": int(self._reliable_watchdog_nacks),
                "forced_skips": int(self._reliable_forced_skips),
                "watchdog_reconnects": int(self._watchdog_reconnects),
                "session_id": None if self._reliable_session_id is None else int(self._reliable_session_id),
                "decode_queued_bytes": int(self._notify_decode_queued_bytes),
                "decode_peak_bytes": int(self._notify_decode_peak_bytes),
                "decode_errors": int(self._notify_decode_errors),
            }

    @staticmethod
    def _percentile(values, q: float) -> float:
        values = sorted(float(v) for v in values if np.isfinite(v) and v >= 0.0)
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        pos = (len(values) - 1) * float(np.clip(q, 0.0, 1.0))
        lo = int(pos)
        hi = min(len(values) - 1, lo + 1)
        frac = pos - lo
        return values[lo] * (1.0 - frac) + values[hi] * frac

    def adaptive_timing(self) -> dict:
        with self._timing_lock:
            gaps = list(self._notify_gap_samples)
            ewma = float(self._notify_gap_ewma_s)
        p95 = self._percentile(gaps, 0.95)
        # Before enough samples are learned, retain V15-like behavior but avoid
        # a 100 ms NACK loop that can overwhelm a slow Windows adapter.
        learned = len(gaps) >= 12
        base = max(0.024, p95, ewma * 1.20)
        # ACK is a tiny write-without-response and must stay prompt even when
        # Windows batches DATA notifications. Stretching ACK to 350 ms made the
        # ESP32 retain too many otherwise healthy blocks during long recordings.
        ack_interval = BLE_RELIABLE_ACK_MAX_INTERVAL_S
        nack_repeat = min(BLE_ADAPTIVE_NACK_MAX_S, max(BLE_RELIABLE_NACK_REPEAT_S, base * 1.6))
        hole_reconnect = min(
            BLE_ADAPTIVE_HOLE_RECONNECT_MAX_S,
            max(3.0, 1.0 + base * 10.0),
        )
        stall_reconnect = min(
            BLE_ADAPTIVE_STALL_RECONNECT_MAX_S,
            max(4.0, 1.5 + base * 14.0),
        )
        hole_timeout = min(
            BLE_ADAPTIVE_HOLE_TIMEOUT_MAX_S,
            max(12.0, hole_reconnect * 2.5),
        )
        reconnect_cooldown = min(15.0, max(6.0, hole_reconnect * 1.5))
        if not learned:
            profile = "learning"
        elif p95 < 0.08:
            profile = "fast"
        elif p95 < 0.18:
            profile = "normal"
        else:
            profile = "batched"
        self._adaptive_profile = profile
        ack_every = BLE_RELIABLE_ACK_EVERY_BLOCKS
        return {
            "profile": profile,
            "samples": len(gaps),
            "p95_s": float(p95),
            "ewma_s": float(ewma),
            "ack_interval_s": float(ack_interval),
            "ack_every_blocks": int(ack_every),
            "nack_repeat_s": float(nack_repeat),
            "hole_reconnect_s": float(hole_reconnect),
            "stall_reconnect_s": float(stall_reconnect),
            "hole_timeout_s": float(hole_timeout),
            "reconnect_cooldown_s": float(reconnect_cooldown),
        }

    def _decode_reliable_bytes_locked(self, incoming: bytes):
        """Return ordered original ADS payloads plus ACK/NACK control packets."""
        if incoming:
            self._reliable_rx_buf.extend(incoming)
        ordered_payloads = []
        control_packets = []
        magic = BLE_BLOCK_MAGIC

        while len(self._reliable_rx_buf) >= 2:
            idx = self._reliable_rx_buf.find(magic)
            if idx < 0:
                keep = 1 if self._reliable_rx_buf[-1:] == magic[:1] else 0
                drop = len(self._reliable_rx_buf) - keep
                if drop > 0:
                    self._reliable_sync_drop += drop
                    del self._reliable_rx_buf[:drop]
                break
            if idx > 0:
                self._reliable_sync_drop += idx
                del self._reliable_rx_buf[:idx]
            if len(self._reliable_rx_buf) < BLE_BLOCK_HEADER_BYTES:
                break

            version = self._reliable_rx_buf[2]
            flags = self._reliable_rx_buf[3]
            frame_count = self._reliable_rx_buf[16]
            payload_len = struct.unpack_from("<H", self._reliable_rx_buf, 18)[0]
            gap_marker = bool(flags & 0x04)
            normal_v1 = (
                version == BLE_BLOCK_VERSION_V1
                and 1 <= frame_count <= BLE_V1_FRAMES_PER_BLOCK
                and payload_len == frame_count * FRAME_BYTES
            )
            compact_v2 = (
                version == BLE_BLOCK_VERSION_V2
                and 1 <= frame_count <= BLE_V2_FRAMES_PER_BLOCK
                and payload_len == frame_count * BLE_COMPACT_FRAME_BYTES
            )
            valid_shape = (
                payload_len <= BLE_BLOCK_MAX_PAYLOAD_BYTES
                and (
                    (gap_marker and frame_count == 0 and payload_len == 0)
                    or normal_v1
                    or compact_v2
                )
            )
            if not valid_shape:
                self._reliable_sync_drop += 1
                del self._reliable_rx_buf[0]
                continue

            total = BLE_BLOCK_HEADER_BYTES + payload_len + BLE_BLOCK_CRC_BYTES
            if len(self._reliable_rx_buf) < total:
                break
            packet = bytes(self._reliable_rx_buf[:total])
            rx_crc = struct.unpack_from("<H", packet, total - 2)[0]
            calc_crc = crc16_ccitt(packet[:-2])
            if rx_crc != calc_crc:
                self._reliable_block_crc_bad += 1
                del self._reliable_rx_buf[0]
                continue
            del self._reliable_rx_buf[:total]

            session_id = struct.unpack_from("<I", packet, 4)[0]
            block_seq = struct.unpack_from("<I", packet, 8)[0]
            payload = packet[BLE_BLOCK_HEADER_BYTES:BLE_BLOCK_HEADER_BYTES + payload_len]
            if not gap_marker and version == BLE_BLOCK_VERSION_V2:
                try:
                    payload = expand_compact_ble_payload(payload, frame_count)
                except Exception:
                    self._reliable_sync_drop += payload_len
                    continue

            if self._reliable_session_id is None:
                self._reliable_session_id = session_id
                self._reliable_accept_any_session = False
            elif session_id != self._reliable_session_id:
                # A reconnect may follow either a short radio interruption or
                # a complete C3 reboot. Accept the first session seen after a
                # reconnect; during a stable connection, only a newer session
                # may replace the current recording.
                if self._reliable_accept_any_session or session_id > self._reliable_session_id:
                    self._reliable_session_id = session_id
                    self._reliable_expected_block = 0
                    self._reliable_pending.clear()
                    self._reliable_last_ack_sent = 0xFFFFFFFF
                    self._reliable_last_ack_time = 0.0
                    self._reliable_last_nack = None
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                    self._reliable_accept_any_session = False
                else:
                    self._reliable_duplicates += 1
                    continue
            else:
                self._reliable_accept_any_session = False
            self._reliable_blocks_received += 1
            if flags & 0x01:
                self._reliable_retransmitted_received += 1

            expected = self._reliable_expected_block
            if block_seq < expected:
                self._reliable_duplicates += 1
                now = time.monotonic()
                if expected > 0 and (now - self._reliable_last_ack_time) >= self.adaptive_timing()["ack_interval_s"]:
                    ack_seq = expected - 1
                    control_packets.append((
                        self._make_reliable_control_packet(BLE_CTRL_ACK, self._reliable_session_id or 0, ack_seq),
                        "ack",
                    ))
                    self._reliable_last_ack_sent = ack_seq
                    self._reliable_last_ack_time = now
                continue

            if block_seq not in self._reliable_pending:
                if block_seq > expected:
                    self._reliable_out_of_order += 1
                self._reliable_pending[block_seq] = (payload, flags)
                self._reliable_max_pending = max(
                    self._reliable_max_pending, len(self._reliable_pending)
                )

            if block_seq > expected:
                if self._reliable_gap_sequence != expected:
                    self._reliable_gap_sequence = expected
                    self._reliable_gap_first_seen = time.monotonic()
                first_missing = expected
                last_missing = min(block_seq - 1, expected + 255)
                now = time.monotonic()
                last_nack = self._reliable_last_nack
                should_send_nack = (
                    last_nack is None
                    or last_nack[0] != first_missing
                    or (now - last_nack[2]) >= self.adaptive_timing()["nack_repeat_s"]
                )
                if should_send_nack:
                    # The first out-of-order block already defines the complete
                    # missing prefix. Later blocks behind the same hole must not
                    # generate one extra GATT write each.
                    if last_nack is not None and last_nack[0] == first_missing:
                        last_missing = last_nack[1]
                    control_packets.append((
                        self._make_reliable_control_packet(
                            BLE_CTRL_NACK_RANGE,
                            self._reliable_session_id or 0,
                            first_missing,
                            last_missing,
                        ),
                        "nack",
                    ))
                    self._reliable_last_nack = (first_missing, last_missing, now)

            delivered_now = 0
            while self._reliable_expected_block in self._reliable_pending:
                seq = self._reliable_expected_block
                block_payload, block_flags = self._reliable_pending.pop(seq)
                if block_flags & 0x04:
                    self._reliable_gap_markers += 1
                elif block_payload:
                    ordered_payloads.append(block_payload)
                    self._reliable_blocks_delivered += 1
                self._reliable_expected_block += 1
                delivered_now += 1

            if delivered_now:
                self._reliable_last_delivery_monotonic = time.monotonic()
                if self._reliable_expected_block in self._reliable_pending:
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                elif self._reliable_pending:
                    next_pending = min(self._reliable_pending)
                    if next_pending > self._reliable_expected_block:
                        if self._reliable_gap_sequence != self._reliable_expected_block:
                            self._reliable_gap_sequence = self._reliable_expected_block
                            self._reliable_gap_first_seen = time.monotonic()
                    else:
                        self._reliable_gap_sequence = None
                        self._reliable_gap_first_seen = 0.0
                else:
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                highest = self._reliable_expected_block - 1
                now = time.monotonic()
                ack_distance = (
                    highest + 1
                    if self._reliable_last_ack_sent == 0xFFFFFFFF
                    else highest - self._reliable_last_ack_sent
                )
                if (
                    ack_distance >= self.adaptive_timing()["ack_every_blocks"]
                    or (now - self._reliable_last_ack_time) >= self.adaptive_timing()["ack_interval_s"]
                    or gap_marker
                ):
                    control_packets.append((
                        self._make_reliable_control_packet(BLE_CTRL_ACK, self._reliable_session_id or 0, highest),
                        "ack",
                    ))
                    self._reliable_last_ack_sent = highest
                    self._reliable_last_ack_time = now
                    self._reliable_last_nack = None

        return ordered_payloads, control_packets

    def _on_data(self, _characteristic, data):
        payload = bytes(data)
        if not payload:
            return
        now = time.monotonic()
        with self._timing_lock:
            if self._last_notify_monotonic is not None:
                gap = max(0.0, now - self._last_notify_monotonic)
                self._notify_gap_last_s = gap
                self._notify_gap_max_s = max(self._notify_gap_max_s, gap)
                if 0.0 < gap <= BLE_ADAPTIVE_LEARN_MAX_GAP_S:
                    self._notify_gap_samples.append(gap)
                    if self._notify_gap_ewma_s <= 0.0:
                        self._notify_gap_ewma_s = gap
                    else:
                        self._notify_gap_ewma_s = 0.90 * self._notify_gap_ewma_s + 0.10 * gap
                if gap >= DISPLAY_JITTER_LONG_GAP_S:
                    self._notify_gap_over_100ms += 1
                    self._notify_gap_events.append((now, gap))
            self._last_notify_monotonic = now
            self._notify_burst_max_bytes = max(self._notify_burst_max_bytes, len(payload))

        # Return to Bleak/Windows immediately. The decoder thread performs all
        # CRC/reassembly/compact expansion and generates ACK/NACK independently
        # of Qt painting, PSD, channel dialogs and disk activity.
        self._enqueue_notify_for_decode(payload)

    async def _reliable_watchdog_loop(self, client):
        """Repair holes without intentionally restarting a healthy BLE session."""
        try:
            while (
                not self._closing
                and client is self._client
                and bool(getattr(client, "is_connected", False))
            ):
                await asyncio.sleep(BLE_RELIABLE_WATCHDOG_INTERVAL_S)
                now = time.monotonic()
                payloads = []
                controls = []
                with self._reliable_lock:
                    expected = int(self._reliable_expected_block)
                    pending_keys = sorted(self._reliable_pending)
                    has_hole = bool(pending_keys and pending_keys[0] > expected)
                    if has_hole:
                        if self._reliable_gap_sequence != expected:
                            self._reliable_gap_sequence = expected
                            self._reliable_gap_first_seen = now
                        age = max(0.0, now - self._reliable_gap_first_seen)
                        first_available = int(pending_keys[0])
                        last_pending = min(pending_keys[-1] - 1, expected + 63)
                        last_nack = self._reliable_last_nack
                        if (
                            last_nack is None
                            or last_nack[0] != expected
                            or (now - last_nack[2]) >= self.adaptive_timing()["nack_repeat_s"]
                        ):
                            controls.append((
                                self._make_reliable_control_packet(
                                    BLE_CTRL_NACK_RANGE,
                                    self._reliable_session_id or 0,
                                    expected,
                                    max(expected, last_pending),
                                ),
                                "nack",
                            ))
                            self._reliable_last_nack = (
                                expected, max(expected, last_pending), now
                            )
                            self._reliable_watchdog_nacks += 1

                        # Long-run policy: never let one unrecoverable block hold
                        # every newer block until the ESP32 ring overflows. After
                        # a bounded repair window (or high pending pressure), jump
                        # directly to the first block we really have. ADS sequence
                        # numbers preserve the exact missing samples as a timeline
                        # gap; acquisition itself keeps running.
                        if (
                            age >= BLE_RELIABLE_HOLE_FAILOPEN_S
                            or len(self._reliable_pending) >= BLE_RELIABLE_FORCE_SKIP_PENDING
                        ):
                            skipped = max(1, first_available - expected)
                            self._reliable_expected_block = first_available
                            self._reliable_forced_skips += skipped
                            self._reliable_gap_markers += skipped
                            self._reliable_gap_sequence = None
                            self._reliable_gap_first_seen = 0.0
                            while self._reliable_expected_block in self._reliable_pending:
                                seq = self._reliable_expected_block
                                block_payload, block_flags = self._reliable_pending.pop(seq)
                                if block_flags & 0x04:
                                    self._reliable_gap_markers += 1
                                elif block_payload:
                                    payloads.append(block_payload)
                                    self._reliable_blocks_delivered += 1
                                self._reliable_expected_block += 1
                            highest = self._reliable_expected_block - 1
                            controls.append((
                                self._make_reliable_control_packet(
                                    BLE_CTRL_ACK,
                                    self._reliable_session_id or 0,
                                    highest,
                                ),
                                "ack",
                            ))
                            self._reliable_last_ack_sent = highest
                            self._reliable_last_ack_time = now
                            self._reliable_last_nack = None
                            self._reliable_last_delivery_monotonic = now

                    # V18 deliberately does not call client.disconnect() merely
                    # because DATA is temporarily quiet. Real disconnections still
                    # enter _on_disconnected() and use the normal reconnect loop.
                    # This prevents long sleep captures from looking like the app
                    # restarted itself after a transient Windows scheduling stall.

                if payloads:
                    joined = b"".join(payloads)
                    with self._data_lock:
                        self._data_chunks.append(joined)
                        self._queued_data_bytes += len(joined)
                for packet, kind in controls:
                    await self._send_reliable_control(packet, kind)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.info.emit(f"BLE 看门狗异常：{exc}")

    def set_streaming_hint(self, active: bool):
        self._streaming_hint = bool(active)
        self._streaming_hint_started_monotonic = (
            time.monotonic() if self._streaming_hint else 0.0
        )

    def timing_metrics(self) -> Tuple[float, float, int, int]:
        with self._timing_lock:
            return (
                float(self._notify_gap_last_s),
                float(self._notify_gap_max_s),
                int(self._notify_burst_max_bytes),
                int(self._notify_gap_over_100ms),
            )

    def recent_gap_events(self):
        with self._timing_lock:
            return list(self._notify_gap_events)

    def reset_timing_metrics(self):
        with self._timing_lock:
            self._last_notify_monotonic = None
            self._notify_gap_last_s = 0.0
            self._notify_gap_max_s = 0.0
            self._notify_burst_max_bytes = 0
            self._notify_gap_over_100ms = 0
            self._notify_gap_events.clear()
            self._notify_gap_samples.clear()
            self._notify_gap_ewma_s = 0.0
            self._adaptive_profile = "learning"

    def queued_data_bytes(self) -> int:
        with self._data_lock:
            return int(self._queued_data_bytes)

    def drain_data(self, max_bytes: int = 131072) -> bytes:
        """Return up to max_bytes without posting per-notify Qt events."""
        limit = max(1, int(max_bytes))
        parts = []
        taken = 0
        with self._data_lock:
            while self._data_chunks and taken < limit:
                chunk = self._data_chunks.popleft()
                room = limit - taken
                if len(chunk) <= room:
                    parts.append(chunk)
                    taken += len(chunk)
                else:
                    parts.append(chunk[:room])
                    self._data_chunks.appendleft(chunk[room:])
                    taken += room
                    break
            self._queued_data_bytes = max(0, self._queued_data_bytes - taken)
        return b"".join(parts)

    def clear_data(self, reset_reliable: bool = False):
        """Clear GUI delivery bytes without silently discarding BLE protocol state.

        Reliable block state is preserved by default.  Resetting it during a
        mode change or automatic reconnect can turn already-retained blocks into
        apparent frame loss.  A fresh recording explicitly requests a reset.
        """
        with self._data_lock:
            self._data_chunks.clear()
            self._queued_data_bytes = 0
        if reset_reliable:
            self._clear_notify_decode_queue()
            self.reset_reliable_state(reset_metrics=True)

    def _publish_status(self, payload: bytes):
        payload = bytes(payload)
        if len(payload) == 12 and payload[:1] == b"\xBC":
            try:
                self.status_queue.put_nowait(payload)
            except queue.Full:
                try:
                    self.status_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.status_queue.put_nowait(payload)
                except queue.Full:
                    pass
        self.status_received.emit(payload)

    def _on_status(self, _characteristic, data):
        self._publish_status(bytes(data))

    def _on_response(self, _characteristic, data):
        try:
            packet = decode_packet(bytes(data))
        except ProtocolError as exc:
            self.info.emit(f"BLE RESPONSE 无效：{exc}")
            return
        waiter = self._response_waiters.pop(packet.request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(packet)

    async def _request(self, message_type: int, payload: bytes = b"", timeout: float = 3.0):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        request_id = self._next_request_id
        self._next_request_id = 1 if request_id >= 0xFFFF else request_id + 1
        waiter = self._loop.create_future()
        self._response_waiters[request_id] = waiter
        try:
            await client.write_gatt_char(
                BLE_CONTROL_UUID,
                encode_packet(message_type, request_id, payload),
                response=True,
            )
            packet = await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            self._response_waiters.pop(request_id, None)
        if packet.message_type != (MSG_RESPONSE | message_type):
            raise RuntimeError(
                f"BLE 响应类型不匹配：0x{packet.message_type:02X}"
            )
        return packet.payload

    def request_blocking(self, message_type: int, payload: bytes = b"", timeout: float = 3.0):
        future = self._submit(self._request(message_type, bytes(payload), timeout))
        return bytes(future.result(timeout=max(0.5, timeout + 0.5)))

    async def _status_poll_loop(self, client):
        try:
            while (
                not self._closing
                and client is self._client
                and bool(getattr(client, "is_connected", False))
            ):
                # STATUS reads share the GATT transaction path with DATA
                # notifications on Windows.  Poll slowly; the 48-byte EEG frame
                # already carries the real-time sequence/queue diagnostics.
                await asyncio.sleep(BLE_STATUS_POLL_INTERVAL_S)
                if self._streaming_hint:
                    # The STATUS characteristic remains subscribed, so ACKs and
                    # firmware-pushed status still arrive. Skipping active reads
                    # removes a periodic source of Windows GATT head-of-line blocking.
                    continue
                try:
                    payload = bytes(await client.read_gatt_char(BLE_STATUS_UUID))
                    self._publish_status(payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.info.emit(f"BLE 状态轮询失败：{exc}")
        except asyncio.CancelledError:
            pass

    def _on_disconnected(self, client):
        if client is not self._client:
            return
        self._client = None
        if self._status_poll_task is not None:
            self._status_poll_task.cancel()
            self._status_poll_task = None
        if self._reliable_watchdog_task is not None:
            self._reliable_watchdog_task.cancel()
            self._reliable_watchdog_task = None
        should_reconnect = (
            not self._closing
            and not self._manual_disconnect
            and self._desired_key is not None
        )
        self.disconnected.emit("BLE 链路意外断开", should_reconnect)
        if should_reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(self._desired_key))

    async def _reconnect_loop(self, key: str):
        delay = 1.0
        while not self._closing and self._desired_key == key and not self._manual_disconnect:
            self.info.emit(f"BLE 将在 {delay:.0f} 秒后自动重连…")
            await asyncio.sleep(delay)
            try:
                await self._connect_to_device(key, reconnected=True)
                if self._client is not None and bool(getattr(self._client, "is_connected", False)):
                    return
            except Exception as exc:
                self.info.emit(f"BLE 重连失败：{exc}")
            delay = min(5.0, delay * 2.0)

    def write_blocking(self, data: bytes, timeout: float = 3.0):
        future = self._submit(self._write(bytes(data)))
        return future.result(timeout=max(0.5, float(timeout)))

    def read_status_blocking(self, timeout: float = 1.0) -> bytes:
        """Read STATUS directly when a notification was lost during setup."""
        future = self._submit(self._read_status())
        return bytes(future.result(timeout=max(0.5, float(timeout))))

    async def _read_status(self) -> bytes:
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        lock = self._gatt_write_lock
        if lock is None:
            raise RuntimeError("BLE GATT 锁未就绪")
        async with lock:
            return bytes(await client.read_gatt_char(BLE_STATUS_UUID))

    async def _write(self, data: bytes):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        if not data:
            return
        lock = self._gatt_write_lock
        if lock is None:
            raise RuntimeError("BLE 写入锁未就绪")
        async with lock:
            await client.write_gatt_char(BLE_CONTROL_UUID, data, response=True)

    def disconnect_blocking(self, timeout: float = 4.0):
        self._desired_key = None
        self._manual_disconnect = True
        future = self._submit(self._disconnect_current())
        return future.result(timeout=max(1.0, float(timeout)))

    async def _disconnect_current(self):
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._status_poll_task is not None:
            self._status_poll_task.cancel()
            self._status_poll_task = None
        if self._reliable_watchdog_task is not None:
            self._reliable_watchdog_task.cancel()
            self._reliable_watchdog_task = None
        client = self._client
        self._client = None
        if client is not None:
            try:
                if bool(getattr(client, "is_connected", False)):
                    try:
                        await client.stop_notify(BLE_DATA_UUID)
                    except Exception:
                        pass
                    try:
                        await client.stop_notify(BLE_STATUS_UUID)
                        await client.stop_notify(BLE_RESPONSE_UUID)
                    except Exception:
                        pass
                    await client.disconnect()
            finally:
                self.disconnected.emit("BLE 已断开", False)

    def shutdown(self):
        if not self.isRunning():
            return
        self._closing = True
        try:
            future = self._submit(self._disconnect_current())
            future.result(timeout=3.0)
        except Exception:
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.wait(3000)


class MainWindow(QtWidgets.QMainWindow):
    api_gui_request = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("全域智能 | ADS1299 EEG 工作站 | 固件 V19 / 通信协议 V1")
        self.resize(1500, 920)

        self.gain = 24  # legacy/global command value
        self.channel_gains = np.full(CHANNELS, 24, dtype=np.int16)
        self.channel_names = [f"CH{index}" for index in range(1, CHANNELS + 1)]
        self.channel_enabled = np.array([True] * 5 + [False] * 3, dtype=bool)
        self.channel_bias = np.array([True] * 5 + [False] * 3, dtype=bool)
        self.reference_mode = REFERENCE_SRB1
        self.channel_srb2 = np.zeros(CHANNELS, dtype=bool)
        self.lsb_uv = self.calc_lsb_uv()
        self.ring = RingBuffer(CHANNELS, FS * 90)            # untouched input-referred uV
        self.filtered_ring = RingBuffer(CHANNELS, FS * 90)   # continuous causal display chain
        self.parser = AdsFrameParser(self.channel_lsb_uv)
        self.filter_generation = 0
        self.filter_worker: Optional[LiveFilterWorker] = None
        self.filter_batches_applied = 0
        self.filter_stale_batches = 0
        self.stream_server: Optional[LocalStreamServer] = None
        self.api_gui_request.connect(self._handle_api_gui_request)
        self.stream_api_errors = 0
        self.ser: Optional[serial.Serial] = None
        self.serial_worker: Optional[SerialTransportWorker] = None
        self.serial_control_read_active = False
        self.serial_buffer_configured = False
        self.serial_buffer_error = ""
        self.active_transport: Optional[str] = None
        self.transport_connecting = False
        self.ble_worker: Optional[BleTransportWorker] = None
        self.ble_connected = False
        # Host-side BLE staging buffer.  Reliable blocks are already ordered
        # in BleTransportWorker; this buffer only coalesces small frame groups so
        # the GUI thread performs fewer parser/filter calls.
        self.ble_rx_buffer = bytearray()
        self.ble_batch_started_monotonic: Optional[float] = None
        self.ble_coalesced_batches = 0
        self.ble_catchup_plot_skips = 0
        self.ble_psd_skips = 0
        self.ble_device_name = ""
        self.ble_device_address = ""
        self.ble_peer_mtu = 23
        self.ble_status = {}
        # Firmware counters are cumulative. Keep the delta from the most recent
        # STATUS update so one harmless event does not poison the verdict for an
        # entire overnight recording.
        self.ble_status_delta = {}
        self.ble_low_mtu_warned = False
        self.ble_protocol_warned = False
        self.ble_supports_srb2 = False
        self.ble_reference_profile = "unknown"
        self.streaming = False
        self.impedance_active = False
        self.impedance_mask = 0
        self.impedance_dialog: Optional[QtWidgets.QDialog] = None
        self.impedance_checks = []
        self.impedance_value_labels = []
        self.impedance_quality_labels = []
        self.impedance_series_spin: Optional[QtWidgets.QDoubleSpinBox] = None
        self.raw_file = None  # legacy compatibility; writes use AsyncRawWriter
        self.raw_writer = AsyncRawWriter()
        self.raw_recording_enabled = False
        self.raw_write_errors = 0
        self.raw_path = ""
        self.raw_bytes = 0
        self.recording_session_id = ""
        self.recording_manifest_path = ""
        self.recording_segment_index = 0
        self.offline_uv: Optional[np.ndarray] = None
        self.offline_valid: Optional[np.ndarray] = None
        self.offline_seq: Optional[np.ndarray] = None
        self.offline_mode: Optional[np.ndarray] = None
        self.offline_end = 0

        self.packet_count = 0
        self.live_sample_count = 0
        self.status_bad = 0
        self.drdy_bad = 0
        self.seq_lost = 0
        self.seq_gap_events = 0
        self.seq_device_lost = 0
        self.seq_host_lost = 0
        self.timeline_gap_samples = 0
        self.timeline_gap_events = 0
        self.timeline_large_discontinuities = 0
        self.live_timeline_sample_count = 0
        self.backlog_events = 0
        self.queue_drop_hints = 0
        self.saturation_samples = 0
        self.saturation_channel_samples = np.zeros(CHANNELS, dtype=np.int64)
        self.last_visible_saturated_channels: Tuple[int, ...] = tuple()
        self.last_seq: Optional[int] = None
        self.last_queue_drop_low = 0
        self.first_seq: Optional[int] = None
        self.first_clock: Optional[float] = None
        self.fs_est = np.nan
        self.current_mode = 0
        # One-click internal-short toggle restores the last normal EEG mode.
        self.mode_before_internal_short = 0
        self._syncing_internal_short_button = False
        self.last_read_us = 0
        self.max_read_us = 0
        self.last_pending = 0
        self.last_queue_depth = 0
        self.last_serial_waiting_bytes = 0
        self.live_lag_s = 0.0
        self._poll_serial_busy = False
        self._transport_repoll_pending = False
        self.transport_max_turn_ms = 0.0
        self.transport_last_turn_ms = 0.0
        self.transport_peak_pending_bytes = 0
        self.serial_catchup_skips = 0
        self._plot_update_busy = False
        self.plot_errors = 0
        self._last_live_plot_packet = -1
        # Live display playback is intentionally delayed by a small amount so
        # Windows BLE notification gaps are absorbed without touching raw data.
        self.display_cursor_sample: Optional[float] = None
        self.display_last_tick = time.monotonic()
        self.display_target_delay_samples = int(round(DISPLAY_JITTER_BASE_TARGET_S * FS))
        self.display_startup_samples = int(round(DISPLAY_JITTER_STARTUP_S * FS))
        self.display_min_reserve_samples = max(1, int(round(DISPLAY_JITTER_MIN_RESERVE_S * FS)))
        self.display_buffer_started = False
        self.display_buffer_state = "priming"
        self.display_buffer_underruns = 0  # event count, not paint-tick count
        self.display_low_latency_resyncs = 0
        self.display_rebuffer_events = 0
        self.display_rebuffer_started_at: Optional[float] = None
        self.display_rebuffer_last_s = 0.0
        self.display_rebuffer_max_s = 0.0
        self.display_delay_s = 0.0
        self.display_reserve_samples = 0
        self.display_last_end_sample = -1
        self.render_gap_last_ms = 0.0
        self.render_gap_max_ms = 0.0
        self.render_gap_over_100ms = 0
        self._last_render_monotonic: Optional[float] = None
        self.session_started_monotonic: Optional[float] = None
        self._last_single_y_range = None
        self._last_channel_y_ranges = [None] * CHANNELS
        self._plot_time_cache = {}
        self._last_range_status_text = None
        self._last_filter_status_text = None
        self._last_single_title = None
        self._last_single_channel_status = None
        self.latest_alpha_power = np.nan
        self.latest_alpha_peak = np.nan
        self.latest_alpha_rel = np.nan
        self.latest_raw_rms = np.nan
        self.latest_filtered_rms = np.nan
        self.latest_raw_pp = np.nan
        self.latest_line_ratio = np.nan
        self.latest_valid_ratio = np.nan
        self.latest_window_good = False
        self.latest_window_reason = "尚未分析"
        self.open_alpha = np.nan
        self.closed_alpha = np.nan
        self.alpha_capture_kind: Optional[str] = None
        self.alpha_capture_start = 0.0
        self.alpha_capture_values: List[float] = []

        # Display chain defaults must match the toolbar: 5-50 Hz plus power-line
        # rejection.  At 250 SPS a physical 150 Hz component aliases to 100 Hz,
        # so the 100 Hz section also suppresses the third harmonic after sampling.
        self.sos_display_band = signal.butter(2, [5.0, 50.0], btype="bandpass", fs=FS, output="sos")
        notch_sections = []
        for notch_hz in (50.0, 100.0):
            notch_b, notch_a = signal.iirnotch(notch_hz, 30.0, fs=FS)
            notch_sections.append(signal.tf2sos(notch_b, notch_a))
        self.sos_notch = np.vstack(notch_sections)
        # A lower beta keeps the spectrum stable without making changes appear
        # several seconds late (the old 0.85 setting felt stuck in live use).
        self.psd_smooth_beta = 0.65
        # PSD owns a private single-thread pool. It cannot compete by spawning
        # extra jobs in Qt's global pool, and psd_worker_busy keeps it strictly
        # single-flight. A missed refresh tick is preferable to any acquisition
        # or waveform delay.
        self.psd_pool = QtCore.QThreadPool(self)
        self.psd_pool.setMaxThreadCount(1)
        self.psd_pool.setExpiryTimeout(5000)
        self.psd_worker_busy = False
        self.psd_request_id = 0
        self.psd_last_signature = None
        self._last_nav_update = 0.0
        self.filter_worker = LiveFilterWorker(
            self.sos_display_band, self.sos_notch, True
        )
        self.filter_worker.start()
        self.reset_processing_state()

        self._build_omni_ui()
        self.stream_server = LocalStreamServer(
            stop_handler=self._api_stop_measurement,
            export_handler=self._api_export_bdf,
        )
        try:
            self.stream_server.start()
        except Exception as exc:
            print(f"Local EEG stream API unavailable: {exc}", file=sys.stderr)
            self.stream_server = None
        if BLE_AVAILABLE:
            self.ble_worker = BleTransportWorker(self)
            self.ble_worker.scan_started.connect(self.on_ble_scan_started)
            self.ble_worker.scan_finished.connect(self.on_ble_scan_finished)
            self.ble_worker.connecting.connect(self.on_ble_connecting)
            self.ble_worker.connected.connect(self.on_ble_connected)
            self.ble_worker.disconnected.connect(self.on_ble_disconnected)
            # DATA is drained directly from the worker's thread-safe queue.
            # Do not enqueue one Qt signal event for every BLE notification.
            self.ble_worker.status_received.connect(self.on_ble_status)
            self.ble_worker.info.connect(self.on_ble_info)
            self.ble_worker.error.connect(self.on_ble_error)
            self.ble_worker.start()
        self.refresh_ports()

        self.serial_timer = QtCore.QTimer(self)
        self.serial_timer.timeout.connect(self.poll_transport)
        self.serial_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.serial_timer.start(SERIAL_POLL_INTERVAL_MS)

        self.filter_result_timer = QtCore.QTimer(self)
        self.filter_result_timer.timeout.connect(self.drain_filter_results)
        self.filter_result_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.filter_result_timer.start(FILTER_RESULT_POLL_MS)

        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.timeout.connect(self.update_fast_plots)
        self.plot_timer.setTimerType(QtCore.Qt.PreciseTimer)
        # Start with the proven USB cadence. _apply_transport_timing switches
        # to V8's 20 FPS cadence when BLE is selected/connected.
        self.plot_timer.start(SERIAL_PLOT_INTERVAL_MS)

        self.psd_timer = QtCore.QTimer(self)
        self.psd_timer.timeout.connect(self.update_psd_and_info)
        # One analysis request per second keeps CPU headroom for serial parsing
        # and 20 FPS waveform painting.
        self.psd_timer.start(PSD_LIVE_REFRESH_MS)

        self.impedance_timer = QtCore.QTimer(self)
        self.impedance_timer.timeout.connect(self.update_impedance_results)
        self.impedance_timer.setInterval(500)

    # ---------------- UI ----------------
    def _build_ui(self):
        pg.setConfigOptions(antialias=False)
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main = QtWidgets.QVBoxLayout(central)

        controls = QtWidgets.QGridLayout()
        main.addLayout(controls)

        row = 0
        controls.addWidget(QtWidgets.QLabel("串口"), row, 0)
        self.port_combo = QtWidgets.QComboBox()
        controls.addWidget(self.port_combo, row, 1)
        self.refresh_btn = QtWidgets.QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        controls.addWidget(self.refresh_btn, row, 2)
        self.connect_btn = QtWidgets.QPushButton("连接")
        self.connect_btn.clicked.connect(self.toggle_connection)
        controls.addWidget(self.connect_btn, row, 3)
        self.start_btn = QtWidgets.QPushButton("开始/保存bin")
        self.start_btn.clicked.connect(self.start_stream)
        controls.addWidget(self.start_btn, row, 4)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_stream)
        self.impedance_btn = QtWidgets.QPushButton("阻抗检测")
        self.impedance_btn.setToolTip("ADS1299 以 6 nA、31.25 Hz 激励并实时估算电极阻抗")
        self.impedance_btn.clicked.connect(self.open_impedance_dialog)
        controls.addWidget(self.stop_btn, row, 5)

        self.mode_combo = QtWidgets.QComboBox()
        for name, _, _ in MODE_ITEMS:
            self.mode_combo.addItem(name)
        controls.addWidget(self.mode_combo, row, 6, 1, 2)
        self.apply_mode_btn = QtWidgets.QPushButton("应用模式")
        self.apply_mode_btn.clicked.connect(self.apply_mode)
        controls.addWidget(self.apply_mode_btn, row, 8)

        controls.addWidget(QtWidgets.QLabel("PGA"), row, 9)
        self.pga_combo = QtWidgets.QComboBox()
        self.pga_combo.addItems([str(x) for x in VALID_GAINS])
        self.pga_combo.setCurrentText(str(self.gain))
        self.pga_combo.currentTextChanged.connect(self.change_pga)
        controls.addWidget(self.pga_combo, row, 10)

        self.filter_check = QtWidgets.QCheckBox("实时滤波显示")
        self.filter_check.setChecked(True)
        controls.addWidget(self.filter_check, row, 11)

        row += 1
        controls.addWidget(QtWidgets.QLabel("通道"), row, 0)
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItems([f"CH{i}" for i in range(1, CHANNELS + 1)])
        self.channel_combo.currentIndexChanged.connect(self.reset_psd_smoothing)
        controls.addWidget(self.channel_combo, row, 1)

        controls.addWidget(QtWidgets.QLabel("时窗(s)"), row, 2)
        self.win_spin = QtWidgets.QDoubleSpinBox()
        self.win_spin.setRange(2.0, 60.0)
        self.win_spin.setDecimals(1)
        self.win_spin.setValue(10.0)
        controls.addWidget(self.win_spin, row, 3)

        controls.addWidget(QtWidgets.QLabel("纵轴±uV(0自动)"), row, 4)
        self.yrange_spin = QtWidgets.QDoubleSpinBox()
        self.yrange_spin.setRange(0.0, 1_000_000.0)
        self.yrange_spin.setDecimals(1)
        self.yrange_spin.setValue(200.0)
        controls.addWidget(self.yrange_spin, row, 5)

        controls.addWidget(QtWidgets.QLabel("PSD上限"), row, 6)
        self.psd_max_spin = QtWidgets.QDoubleSpinBox()
        self.psd_max_spin.setRange(20, FS / 2)
        self.psd_max_spin.setValue(65)
        controls.addWidget(self.psd_max_spin, row, 7)

        self.psd_raw_check = QtWidgets.QCheckBox("PSD显示原始诊断")
        self.psd_raw_check.stateChanged.connect(self.reset_psd_smoothing)
        controls.addWidget(self.psd_raw_check, row, 8)

        self.open_btn = QtWidgets.QPushButton("采集20秒睁眼")
        self.open_btn.clicked.connect(lambda: self.store_alpha(False))
        controls.addWidget(self.open_btn, row, 9)
        self.closed_btn = QtWidgets.QPushButton("采集20秒闭眼")
        self.closed_btn.clicked.connect(lambda: self.store_alpha(True))
        controls.addWidget(self.closed_btn, row, 10)
        self.clear_btn = QtWidgets.QPushButton("清空统计")
        self.clear_btn.clicked.connect(self.clear_stats)
        controls.addWidget(self.clear_btn, row, 11)

        row += 1
        controls.addWidget(QtWidgets.QLabel("bin名"), row, 0)
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID_minuteNN.bin（自动）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("每 60 秒自动切片；完整配置写入 manifest 和 .meta.json")
        controls.addWidget(self.bin_name, row, 1, 1, 4)
        self.import_btn = QtWidgets.QPushButton("导入文件")
        self.import_btn.clicked.connect(self.import_file)
        controls.addWidget(self.import_btn, row, 5)
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offline_slider.setEnabled(False)
        self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        controls.addWidget(self.offline_slider, row, 6, 1, 4)
        self.offline_label = QtWidgets.QLabel("未导入")
        controls.addWidget(self.offline_label, row, 10, 1, 2)

        row += 1
        bias_box = QtWidgets.QGroupBox("BIAS 通道：写入后读取 ADS1299 的 SENSP/SENSN 验证")
        bias_layout = QtWidgets.QHBoxLayout(bias_box)
        self.bias_checks = []
        for i in range(1, CHANNELS + 1):
            cb = QtWidgets.QCheckBox(f"CH{i}")
            cb.setChecked(i <= 5)
            cb.stateChanged.connect(self.update_bias_mask_label)
            self.bias_checks.append(cb)
            bias_layout.addWidget(cb)
        self.bias_mask_label = QtWidgets.QLabel("mask=0x1F")
        bias_layout.addWidget(self.bias_mask_label)
        self.bias_apply_btn = QtWidgets.QPushButton("写入并读回验证")
        self.bias_apply_btn.clicked.connect(self.apply_bias_sensp)
        bias_layout.addWidget(self.bias_apply_btn)
        self.bias_ch15_btn = QtWidgets.QPushButton("CH1-5")
        self.bias_ch15_btn.clicked.connect(lambda: self.set_bias_checks(0x1F))
        bias_layout.addWidget(self.bias_ch15_btn)
        self.bias_all_btn = QtWidgets.QPushButton("全选")
        self.bias_all_btn.clicked.connect(lambda: self.set_bias_checks(0xFF))
        bias_layout.addWidget(self.bias_all_btn)
        self.bias_none_btn = QtWidgets.QPushButton("全不选")
        self.bias_none_btn.clicked.connect(lambda: self.set_bias_checks(0x00))
        bias_layout.addWidget(self.bias_none_btn)
        controls.addWidget(bias_box, row, 0, 1, 12)

        row += 1
        self.status_label = QtWidgets.QLabel("未连接。实时波形使用连续有状态滤波；原始诊断与 Alpha 分析互不影响。")
        self.status_label.setStyleSheet("font-weight: bold;")
        controls.addWidget(self.status_label, row, 0, 1, 12)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        main.addWidget(splitter, 1)

        top_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(top_split)
        bottom_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(bottom_split)
        splitter.setSizes([520, 350])

        self.time_plot = pg.PlotWidget(title="选中通道时域")
        self.time_plot.showGrid(x=True, y=True, alpha=0.25)
        self.time_plot.setLabel("bottom", "Time", units="s")
        self.time_plot.setLabel("left", "Amplitude", units="uV")
        self.time_curve = self.time_plot.plot(pen=pg.mkPen(width=1))
        top_split.addWidget(self.time_plot)

        self.psd_plot = pg.PlotWidget(title="Welch PSD")
        self.psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self.psd_plot.setLabel("bottom", "Frequency", units="Hz")
        self.psd_plot.setLabel("left", "PSD", units="dB uV^2/Hz")
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#111111", width=2.2))
        self.psd_plot.addLine(x=8, pen=pg.mkPen(style=QtCore.Qt.DashLine))
        self.psd_plot.addLine(x=13, pen=pg.mkPen(style=QtCore.Qt.DashLine))
        top_split.addWidget(self.psd_plot)
        top_split.setSizes([900, 560])

        self.stack_plot = pg.PlotWidget(title="CH1-CH8 堆叠波形")
        self.stack_plot.showGrid(x=True, y=True, alpha=0.25)
        self.stack_plot.setLabel("bottom", "Time", units="s")
        self.stack_curves = [self.stack_plot.plot(pen=pg.mkPen(width=1)) for _ in range(CHANNELS)]
        bottom_split.addWidget(self.stack_plot)

        self.info_text = QtWidgets.QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMinimumWidth(430)
        self.info_text.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        bottom_split.addWidget(self.info_text)
        bottom_split.setSizes([1000, 480])

    def _build_omni_ui(self):
        """全域智能 compact review layout with acquisition diagnostics."""
        # Antialiasing eight continuously moving traces is expensive and adds
        # no useful EEG detail at screen resolution.
        pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#424245")
        self.setWindowTitle("全域智能 | ADS1299 EEG 工作站 | V18 | split BLE TX / capture pipeline")
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))
        self.setMinimumSize(1050, 680)
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#f4f5f7; color:#2d2521; font-size:12px; }
            QMenuBar { background:#ffffff; border-bottom:1px solid #d8dde3; padding:2px; }
            QMenuBar::item:selected, QMenu::item:selected { background:#fff0e6; color:#b83c00; }
            QToolBar { background:#ffffff; color:#2d2521; border-bottom:3px solid #ff5a01; spacing:5px; padding:5px 7px; min-height:48px; }
            QToolBar QLabel, QToolBar QCheckBox { color:#2d2521; background:transparent; font-size:13px; font-weight:600; }
            QToolBar QToolButton { color:#2d2521; background:transparent; border:0; font-weight:600; padding:4px 8px; }
            QToolBar QDoubleSpinBox, QToolBar QSpinBox, QToolBar QComboBox, QToolBar QPushButton {
                color:#2d2521; background:#ffffff; border:1px solid #d8dde3; min-height:22px;
            }
            QToolBar QComboBox QAbstractItemView { color:#2d2521; background:#ffffff; }
            QToolButton, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox {
                color:#2d2521; background:#ffffff; border:1px solid #d8dde3; padding:2px 5px; min-height:20px;
            }
            QToolButton:hover, QPushButton:hover { background:#fff4ed; border-color:#ff8b50; }
            QToolButton:pressed, QPushButton:pressed { background:#ffd8c2; }
            QGroupBox { background:#ffffff; color:#ff5a01; font-weight:600; border:1px solid #d8dde3; border-radius:3px; margin-top:7px; padding-top:6px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; background:#ffffff; }
            QTabWidget::pane { border:1px solid #d8dde3; background:#ffffff; }
            QTabBar::tab { background:#ffffff; border:1px solid #d8dde3; border-bottom:0; padding:5px 14px; }
            QTabBar::tab:selected { background:#fff0e6; color:#b83c00; border-color:#ff8b50; }
            QStatusBar { background:#ffffff; border-top:1px solid #d8dde3; }
            QDockWidget::title { background:#ffffff; color:#ff5a01; padding:5px; font-weight:600; }
            QDialog { background:#f4f5f7; }
            QDialog QLabel { color:#2d2521; }
        """)

        # File and view menus retain the existing acquisition functionality.
        file_menu = self.menuBar().addMenu("文件")
        open_action = file_menu.addAction("导入文件…")
        open_action.setShortcut(QtGui.QKeySequence.Open)
        open_action.triggered.connect(self.import_file)
        export_action = file_menu.addAction("导出 CSV…")
        export_action.setShortcut("Ctrl+Shift+S")
        export_action.triggered.connect(self.export_csv)
        format_action = file_menu.addAction("导出 BDF/FIF…")
        format_action.triggered.connect(self.export_biosignal_formats)
        file_menu.addSeparator()
        log_action = file_menu.addAction("打开日志目录")
        log_action.triggered.connect(self.open_log_directory)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close, QtGui.QKeySequence.Quit)
        view_menu = self.menuBar().addMenu("视图")
        mne_action = view_menu.addAction("打开 MNE 浏览器")
        mne_action.triggered.connect(self.open_mne_browser)
        acquire_menu = self.menuBar().addMenu("采集")
        acquire_menu.addAction("连接/断开设备", self.toggle_connection)
        acquire_menu.addAction("开始采集并保存 BIN", self.start_stream)
        acquire_menu.addAction(
            "停止采集", lambda: self.stop_stream(offer_export=True)
        )

        toolbar = self.addToolBar("EEG 工具")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.logo_label = QtWidgets.QLabel()
        self.logo_label.setToolTip("全域智能")
        self.logo_label.setFixedSize(220, 54)
        self.logo_label.setAlignment(QtCore.Qt.AlignCenter)
        # Compose the EEG identity inside the original 220 x 54 logo slot so
        # the P0P1 toolbar geometry and every surrounding control stay put.
        logo_canvas = QtGui.QPixmap(220, 54)
        logo_canvas.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(logo_canvas)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        mark = QtGui.QPixmap(str(APP_ICON_PATH))
        if not mark.isNull():
            painter.drawPixmap(
                4, 4, mark.scaled(
                    46, 46, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
                )
            )
        painter.setPen(QtGui.QColor("#1d1d1f"))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 12, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(57, 5, 155, 25), QtCore.Qt.AlignLeft, "全域智能")
        painter.setPen(QtGui.QColor(OMNI_ORANGE))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 9, QtGui.QFont.DemiBold))
        painter.drawText(
            QtCore.QRect(57, 28, 155, 20),
            QtCore.Qt.AlignLeft,
            "脑电测试 · EEG",
        )
        painter.end()
        self.logo_label.setPixmap(logo_canvas)
        toolbar.addWidget(self.logo_label)
        toolbar.addSeparator()
        toolbar.addAction(open_action)
        export_file_button = QtWidgets.QToolButton()
        export_file_button.setText("导出文件…")
        export_file_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        export_file_menu = QtWidgets.QMenu(export_file_button)
        export_file_menu.addAction(export_action)
        export_file_menu.addAction(format_action)
        export_file_button.setMenu(export_file_menu)
        toolbar.addWidget(export_file_button)
        toolbar.addSeparator()

        self.filter_check = QtWidgets.QCheckBox("滤波后")
        self.filter_check.setChecked(True)
        self.filter_check.stateChanged.connect(self._filter_settings_changed)
        toolbar.addWidget(self.filter_check)
        toolbar.addSeparator()
        self.start_time_spin = QtWidgets.QDoubleSpinBox()
        self.start_time_spin.setRange(0, 86400)
        self.start_time_spin.setDecimals(1)
        self.start_time_spin.setSuffix(" s")
        self.start_time_spin.setKeyboardTracking(False)
        self.start_time_spin.valueChanged.connect(self._start_time_changed)
        self._add_tool_field(toolbar, "开始时间", self.start_time_spin)
        self.win_spin = QtWidgets.QDoubleSpinBox()
        self.win_spin.setRange(1, 60)
        self.win_spin.setValue(10)
        self.win_spin.setSuffix(" s")
        self.win_spin.valueChanged.connect(self._window_changed)
        self._add_tool_field(toolbar, "时间窗", self.win_spin)
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItems(["15", "30", "60"])
        self.speed_combo.setCurrentText("30")
        self._add_tool_field(toolbar, "走纸速度", self.speed_combo, " mm/s")
        self.sensitivity_spin = QtWidgets.QDoubleSpinBox()
        self.sensitivity_spin.setRange(1, 100000)
        self.sensitivity_spin.setValue(100)
        self.sensitivity_spin.setSuffix(" uV/cm")
        self.sensitivity_spin.valueChanged.connect(self.update_fast_plots)
        self._add_tool_field(toolbar, "灵敏度", self.sensitivity_spin)
        self.hp_spin = QtWidgets.QDoubleSpinBox()
        self.hp_spin.setRange(0.1, 30); self.hp_spin.setValue(5); self.hp_spin.setSuffix(" Hz")
        self.lp_spin = QtWidgets.QDoubleSpinBox()
        self.lp_spin.setRange(10, 120); self.lp_spin.setValue(50); self.lp_spin.setSuffix(" Hz")
        self.notch_check = QtWidgets.QCheckBox("50/100 Hz 谐波陷波"); self.notch_check.setChecked(True)
        self.notch_check.setToolTip(
            "级联抑制 50 Hz 和 100 Hz；采样率为 250 SPS 时，150 Hz 会混叠到 100 Hz。"
        )
        self.hp_spin.valueChanged.connect(self._filter_settings_changed)
        self.lp_spin.valueChanged.connect(self._filter_settings_changed)
        self.notch_check.stateChanged.connect(self._filter_settings_changed)
        toolbar.addSeparator()
        toolbar.addAction(mne_action)

        # USB CDC and BLE share one acquisition/parser pipeline.
        self.transport_combo = QtWidgets.QComboBox()
        self.transport_combo.addItem("USB 串口", "serial")
        self.transport_combo.addItem("BLE 无线", "ble")
        self.transport_combo.setMinimumWidth(105)
        self.transport_combo.currentIndexChanged.connect(self.transport_mode_changed)
        self.serial_label = QtWidgets.QLabel("串口")
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(220)
        self.port_combo.setToolTip("先扫描设备，再选择要连接的目标")
        self.refresh_btn = QtWidgets.QPushButton("扫描串口")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn = QtWidgets.QPushButton("打开串口")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.start_btn = QtWidgets.QPushButton("开始采集")
        self.start_btn.clicked.connect(self.start_stream)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.clicked.connect(lambda: self.stop_stream(offer_export=True))
        self.impedance_btn = QtWidgets.QPushButton("阻抗检测")
        self.impedance_btn.setToolTip(
            "ADS1299 以 6 nA、31.25 Hz 激励并实时估算电极阻抗"
        )
        self.impedance_btn.clicked.connect(self.open_impedance_dialog)
        self.internal_short_btn = QtWidgets.QPushButton("内部短接")
        self.internal_short_btn.setCheckable(True)
        self.internal_short_btn.setToolTip(
            "一键把所有已启用 ADS1299 通道切到内部输入短接（MUX=001）；"
            "再次点击会恢复进入短接前的 EEG/BIAS 模式。"
        )
        self.internal_short_btn.toggled.connect(self.toggle_internal_short)
        self.reference_combo = QtWidgets.QComboBox()
        for label, value in REFERENCE_ITEMS:
            self.reference_combo.addItem(label, value)
        self.reference_combo.setCurrentIndex(0)
        self.reference_combo.setMinimumWidth(205)
        self.reference_combo.setToolTip(
            "V19 固定使用 SRB1：每通道信号接 INxP，公共参考接 SRB1。"
        )
        self.apply_reference_btn = QtWidgets.QPushButton("应用参考")
        self.apply_reference_btn.clicked.connect(self.apply_reference_mode)
        self.reference_combo.setEnabled(False)
        self.apply_reference_btn.setVisible(False)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(5, 5, 5, 3)
        layout.setSpacing(3)

        serial_box = QtWidgets.QGroupBox("设备连接与控制")
        serial_layout = QtWidgets.QHBoxLayout(serial_box)
        serial_layout.setContentsMargins(8, 4, 8, 4)
        serial_layout.addWidget(self.transport_combo)
        serial_layout.addWidget(self.serial_label)
        serial_layout.addWidget(self.port_combo, 1)
        serial_layout.addWidget(self.refresh_btn)
        serial_layout.addWidget(self.connect_btn)
        serial_layout.addWidget(self.start_btn)
        serial_layout.addWidget(self.stop_btn)
        serial_layout.addWidget(self.impedance_btn)
        serial_layout.addWidget(self.internal_short_btn)
        serial_layout.addWidget(QtWidgets.QLabel("参考"))
        serial_layout.addWidget(self.reference_combo)
        serial_layout.addWidget(self.apply_reference_btn)
        self.status_label = QtWidgets.QLabel("未扫描")
        self.status_label.setStyleSheet("color:#c94700; font-weight:600;")
        serial_layout.addWidget(self.status_label, 1)
        layout.addWidget(serial_box)

        filter_box = QtWidgets.QGroupBox("滤波设置")
        filter_layout = QtWidgets.QHBoxLayout(filter_box)
        filter_layout.setContentsMargins(8, 4, 8, 4)
        filter_layout.addWidget(QtWidgets.QLabel("高通"))
        self.hp_spin.setMinimumWidth(90)
        filter_layout.addWidget(self.hp_spin)
        filter_layout.addWidget(QtWidgets.QLabel("低通"))
        self.lp_spin.setMinimumWidth(90)
        filter_layout.addWidget(self.lp_spin)
        filter_layout.addWidget(self.notch_check)
        filter_layout.addStretch(1)
        layout.addWidget(filter_box)

        self.view_tabs = QtWidgets.QTabWidget()
        wave_page = QtWidgets.QWidget()
        wave_page_layout = QtWidgets.QVBoxLayout(wave_page)
        wave_page_layout.setContentsMargins(2, 2, 2, 2)
        wave_page_layout.setSpacing(3)
        self.view_tabs.addTab(wave_page, "波形")

        # A dedicated, full-size single-channel view. Double-clicking any
        # channel plot below selects the channel and switches to this tab.
        self.single_channel_index = 0
        single_page = QtWidgets.QWidget()
        single_layout = QtWidgets.QVBoxLayout(single_page)
        single_layout.setContentsMargins(5, 5, 5, 5)
        single_header = QtWidgets.QHBoxLayout()
        single_header.addWidget(QtWidgets.QLabel("放大通道"))
        self.single_channel_combo = QtWidgets.QComboBox()
        self.single_channel_combo.addItems([f"CH{i}" for i in range(1, CHANNELS + 1)])
        self.single_channel_combo.currentIndexChanged.connect(self._single_channel_changed)
        single_header.addWidget(self.single_channel_combo)
        self.single_channel_status = QtWidgets.QLabel("CH1 | 等待数据")
        self.single_channel_status.setStyleSheet("color:#c94700;font-weight:600;")
        single_header.addWidget(self.single_channel_status, 1)
        single_header.addWidget(QtWidgets.QLabel("纵轴 Scale"))
        self.single_scale_spin = QtWidgets.QDoubleSpinBox()
        self.single_scale_spin.setRange(1.0, 100000.0)
        self.single_scale_spin.setDecimals(0)
        self.single_scale_spin.setValue(100.0)
        self.single_scale_spin.setSuffix(" µV")
        self.single_scale_spin.setKeyboardTracking(False)
        self.single_scale_spin.setToolTip(
            "当前放大通道的纵轴半幅；也可在波形上滚动鼠标滚轮调整"
        )
        self.single_scale_spin.valueChanged.connect(self._single_scale_changed)
        single_header.addWidget(self.single_scale_spin)
        back_to_all = QtWidgets.QPushButton("返回八通道")
        back_to_all.clicked.connect(lambda: self.view_tabs.setCurrentIndex(0))
        single_header.addWidget(back_to_all)
        single_layout.addLayout(single_header)
        self.single_plot = pg.PlotWidget(axisItems={"bottom": ClockAxisItem(orientation="bottom")})
        self.single_plot.setBackground("#ffffff")
        self.single_plot.setMenuEnabled(False)
        self.single_plot.setMouseEnabled(x=True, y=False)
        self._scale_viewbox_channels = {}
        self._scale_viewbox_channels[self.single_plot.getViewBox()] = -1
        self.single_plot.getViewBox().installEventFilter(self)
        self.single_plot.showGrid(x=True, y=True, alpha=0.22)
        self.single_plot.setLabel("left", "幅值", units="uV")
        self.single_plot.setLabel("bottom", "时间")
        self.single_zero_line = self.single_plot.addLine(
            y=0, pen=pg.mkPen("#56616b", width=1)
        )
        self.single_curve = self.single_plot.plot(
            pen=pg.mkPen(CHANNEL_COLORS[0], width=2.4), connect="finite"
        )
        self.single_curve.setClipToView(True)
        self.single_curve.setDownsampling(auto=True, method="peak")
        single_layout.addWidget(self.single_plot, 1)
        self.single_nav_plot = pg.PlotWidget()
        self.single_nav_plot.setFixedHeight(62)
        self.single_nav_plot.hideAxis("left")
        self.single_nav_plot.setMouseEnabled(x=True, y=False)
        self.single_nav_plot.getPlotItem().setMenuEnabled(False)
        self.single_nav_plot.setBackground("#ffffff")
        self.single_nav_curve = self.single_nav_plot.plot(
            pen=pg.mkPen("#86868b", width=1)
        )
        self.single_nav_region = pg.LinearRegionItem(
            values=(0, 10),
            movable=True,
            brush=pg.mkBrush(255, 90, 1, 45),
            pen=pg.mkPen(OMNI_ORANGE, width=1.5),
        )
        self.single_nav_region.sigRegionChanged.connect(self._nav_region_changed)
        self.single_nav_plot.addItem(self.single_nav_region)
        self.single_nav_plot.setVisible(False)
        single_layout.addWidget(self.single_nav_plot)
        self.single_tab_index = self.view_tabs.addTab(single_page, "单通道放大")
        self.view_tabs.currentChanged.connect(self.update_fast_plots)
        layout.addWidget(self.view_tabs, 1)

        self.nav_plot = pg.PlotWidget()
        self.nav_plot.setFixedHeight(62)
        self.nav_plot.hideAxis("left")
        self.nav_plot.setMouseEnabled(x=True, y=False)
        self.nav_plot.getPlotItem().setMenuEnabled(False)
        self.nav_plot.setBackground("#ffffff")
        self.nav_curve = self.nav_plot.plot(pen=pg.mkPen("#86868b", width=1))
        self.nav_region = pg.LinearRegionItem(values=(0, 10), movable=True,
            brush=pg.mkBrush(255, 90, 1, 45), pen=pg.mkPen(OMNI_ORANGE, width=1.5))
        self.nav_region.sigRegionChanged.connect(self._nav_region_changed)
        self.nav_plot.addItem(self.nav_region)
        wave_page_layout.addWidget(self.nav_plot)

        self.event_label = QtWidgets.QLabel("  ━  滤波显示副本（不修改原始数据）")
        self.event_label.setFixedHeight(21)
        self.event_label.setStyleSheet("background:#fff0e6;color:#b83c00;border:1px solid #f3c2a5;")
        wave_page_layout.addWidget(self.event_label)

        wave_row = QtWidgets.QWidget()
        wave_layout = QtWidgets.QHBoxLayout(wave_row)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        wave_layout.setSpacing(0)
        channel_panel = QtWidgets.QFrame()
        channel_panel.setFixedWidth(285)
        channel_panel.setStyleSheet("background:#ffffff;border-right:1px solid #d8dde3;")
        channel_layout = QtWidgets.QVBoxLayout(channel_panel)
        channel_layout.setContentsMargins(0, 0, 0, 31)
        channel_layout.setSpacing(0)
        channel_header = QtWidgets.QLabel("通道参数（点击通道修改） / 幅值")
        channel_header.setAlignment(QtCore.Qt.AlignCenter)
        channel_header.setFixedHeight(27)
        channel_header.setStyleSheet("background:#ffffff;color:#ff5a01;border-bottom:3px solid #ff5a01;font-size:14px;font-weight:bold;")
        channel_layout.addWidget(channel_header)
        self.channel_buttons = []
        self.channel_scales = []
        for ch in range(CHANNELS):
            row_widget = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 2, 0)
            row_layout.setSpacing(2)
            button = QtWidgets.QToolButton()
            button.setText(f"CH{ch+1}")
            status_icon = QtGui.QPixmap(11, 11)
            status_icon.fill(QtGui.QColor("#56bd31"))
            button.setIcon(QtGui.QIcon(status_icon))
            button.setIconSize(QtCore.QSize(11, 11))
            button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
            button.setStyleSheet(
                "QToolButton{background:#ffffff;color:#2d2521;border:0;"
                "border-bottom:1px solid #e2e6eb;text-align:left;padding-left:10px;font-size:13px;}"
            )
            button.clicked.connect(lambda _checked=False, index=ch: self.open_channel_settings(index))
            row_layout.addWidget(button, 1)
            self.channel_buttons.append(button)
            scale = QtWidgets.QDoubleSpinBox()
            scale.setRange(1.0, 100000.0)
            scale.setDecimals(0)
            scale.setValue(100.0)
            scale.setSuffix(" µV")
            scale.setToolTip(f"CH{ch+1} 独立纵轴半幅；最大 100000 µV = 0.1 V")
            scale.setKeyboardTracking(False)
            scale.valueChanged.connect(
                lambda value, index=ch: self._channel_scale_changed(index, value)
            )
            row_layout.addWidget(scale)
            self.channel_scales.append(scale)
            channel_layout.addWidget(row_widget, 1)
        self.refresh_channel_parameter_labels()
        wave_layout.addWidget(channel_panel)

        # Each channel gets its own PlotItem and y-range.  This removes the
        # artificial lane offsets and lets every channel use its own amplitude.
        self.wave_widget = pg.GraphicsLayoutWidget()
        self.wave_widget.setBackground("#ffffff")
        self.channel_plots = []
        self.stack_curves = []
        for ch in range(CHANNELS):
            plot = self.wave_widget.addPlot(row=ch, col=0)
            plot.setMenuEnabled(False)
            plot.setMouseEnabled(x=True, y=False)
            if ch > 0:
                plot.setXLink(self.channel_plots[0])
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setLabel("left", f"CH{ch+1}", units="uV")
            if ch < CHANNELS - 1:
                plot.hideAxis("bottom")
            else:
                plot.setLabel("bottom", "时间", units="s")
            curve = plot.plot(pen=pg.mkPen(CHANNEL_COLORS[ch], width=2.0),
                              connect="finite")
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
            self.channel_plots.append(plot)
            self.stack_curves.append(curve)
            self._scale_viewbox_channels[plot.getViewBox()] = ch
            plot.getViewBox().installEventFilter(self)
        self.stack_plot = self.channel_plots[0]
        self.stack_plot.getViewBox().sigXRangeChanged.connect(self._main_range_changed)
        self.wave_widget.scene().sigMouseClicked.connect(self._wave_scene_clicked)
        wave_layout.addWidget(self.wave_widget, 1)
        wave_page_layout.addWidget(wave_row, 1)

        # Compatibility widgets used by the existing acquisition/PSD code.
        # The serial widgets above are the real, visible controls; do not
        # recreate them here or the toolbar would lose its signal bindings.
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID_minuteNN.bin（自动）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("每 60 秒自动切片；完整配置写入 manifest 和 .meta.json")
        self.mode_combo = QtWidgets.QComboBox()
        for name, _, _ in MODE_ITEMS: self.mode_combo.addItem(name)
        self.pga_combo = QtWidgets.QComboBox(); self.pga_combo.addItems([str(x) for x in VALID_GAINS]); self.pga_combo.setCurrentText("24")
        self.channel_combo = QtWidgets.QComboBox(); self.channel_combo.addItems([f"CH{i}" for i in range(1, 9)])
        self.channel_combo.currentIndexChanged.connect(self.reset_psd_smoothing)
        self.psd_raw_check = QtWidgets.QCheckBox("显示未滤波 PSD")
        self.psd_raw_check.setChecked(False)
        self.psd_raw_check.stateChanged.connect(self.reset_psd_smoothing)
        self.psd_max_spin = QtWidgets.QDoubleSpinBox()
        self.psd_max_spin.setRange(10.0, FS / 2)
        self.psd_max_spin.setValue(65.0)
        self.psd_max_spin.setSingleStep(5.0)
        self.psd_max_spin.setSuffix(" Hz")
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        self.offline_label = QtWidgets.QLabel()
        self.bias_checks = [QtWidgets.QCheckBox() for _ in range(8)]
        for i, cb in enumerate(self.bias_checks): cb.setChecked(i < 5)
        self.bias_mask_label = QtWidgets.QLabel()
        self.open_btn = QtWidgets.QPushButton(); self.closed_btn = QtWidgets.QPushButton()
        self.time_plot = pg.PlotWidget(); self.time_curve = self.time_plot.plot()
        self.psd_plot = pg.PlotWidget()
        self.psd_plot.setBackground("#ffffff")
        self.psd_plot.showGrid(x=True, y=True, alpha=0.22)
        self.psd_plot.setLabel("bottom", "频率", units="Hz")
        self.psd_plot.setLabel("left", "PSD", units="dB µV²/Hz")
        self.psd_plot.setTitle("Welch PSD | 等待数据")
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#111111", width=2.2))
        self.psd_plot.addLine(x=8, pen=pg.mkPen("#ff9a5c", style=QtCore.Qt.DashLine))
        self.psd_plot.addLine(x=13, pen=pg.mkPen("#ff9a5c", style=QtCore.Qt.DashLine))
        psd_page = QtWidgets.QWidget()
        psd_layout = QtWidgets.QVBoxLayout(psd_page)
        psd_layout.setContentsMargins(6, 6, 6, 6)
        psd_controls = QtWidgets.QHBoxLayout()
        psd_controls.addWidget(QtWidgets.QLabel("通道"))
        self.channel_combo.setMinimumWidth(90)
        psd_controls.addWidget(self.channel_combo)
        psd_controls.addWidget(self.psd_raw_check)
        psd_controls.addWidget(QtWidgets.QLabel("频率上限"))
        self.psd_max_spin.setMinimumWidth(100)
        psd_controls.addWidget(self.psd_max_spin)
        psd_controls.addStretch(1)
        psd_controls.addWidget(QtWidgets.QLabel("橙色虚线：8–13 Hz Alpha 范围"))
        psd_layout.addLayout(psd_controls)
        psd_layout.addWidget(self.psd_plot, 1)
        # PSD is a movable/floating dock so it can sit beside the waveform or
        # be pulled out as a separate window, like a detachable browser tab.
        self.psd_dock = QtWidgets.QDockWidget("频谱 PSD（可拖动/浮动）", self)
        self.psd_dock.setObjectName("PsdDock")
        self.psd_dock.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea
            | QtCore.Qt.RightDockWidgetArea
            | QtCore.Qt.BottomDockWidgetArea
        )
        self.psd_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetClosable
        )
        self.psd_dock.setWidget(psd_page)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.psd_dock)
        self.psd_toggle_action = view_menu.addAction("显示/隐藏 PSD 窗口")
        self.psd_toggle_action.setCheckable(True)
        self.psd_toggle_action.setChecked(True)
        self.psd_toggle_action.triggered.connect(self.toggle_psd_dock)
        self.psd_dock.visibilityChanged.connect(self.psd_toggle_action.setChecked)

        # Transport diagnostics used to be instantiated but never inserted into
        # the Omni layout.  Give it a real tab so it is always reachable without
        # shrinking, clipping, or otherwise altering the waveform views.
        diagnostics_page = QtWidgets.QWidget()
        diagnostics_layout = QtWidgets.QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(6, 6, 6, 6)
        diagnostics_layout.setSpacing(4)

        diagnostics_header = QtWidgets.QHBoxLayout()
        diagnostics_title = QtWidgets.QLabel("传输与显示诊断")
        diagnostics_title.setStyleSheet("font-size:14px; font-weight:700; color:#ff5a01;")
        diagnostics_header.addWidget(diagnostics_title)
        diagnostics_header.addStretch(1)
        self.diagnostics_pause_btn = QtWidgets.QPushButton("暂停刷新")
        self.diagnostics_pause_btn.setCheckable(True)
        self.diagnostics_pause_btn.setToolTip("暂停后数值和滚动位置保持不变，便于截图或抄录")
        self.diagnostics_pause_btn.toggled.connect(
            lambda checked: self.diagnostics_pause_btn.setText(
                "继续刷新" if checked else "暂停刷新"
            )
        )
        diagnostics_header.addWidget(self.diagnostics_pause_btn)
        diagnostics_copy_btn = QtWidgets.QPushButton("复制诊断信息")
        diagnostics_copy_btn.setToolTip("复制当前全部诊断字段，便于排查 BLE/GUI 卡顿")
        diagnostics_header.addWidget(diagnostics_copy_btn)
        diagnostics_layout.addLayout(diagnostics_header)

        diagnostics_note = QtWidgets.QLabel(
            "三栏紧凑显示；诊断每 0.5 秒刷新一次，不改变滚动位置，也不影响 EEG 数据。"
        )
        diagnostics_note.setWordWrap(False)
        diagnostics_note.setStyleSheet("color:#5b6168;font-size:11px;")
        diagnostics_layout.addWidget(diagnostics_note)

        self.info_text = QtWidgets.QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.info_text.setStyleSheet(
            "QPlainTextEdit{background:#ffffff;color:#242424;border:1px solid #d8dde3;"
            "font-family:Consolas, 'Cascadia Mono', monospace;font-size:10px;padding:4px;}"
        )
        self.info_text.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.info_text.document().setDocumentMargin(2)
        diagnostics_layout.addWidget(self.info_text, 1)
        diagnostics_copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(self.info_text.toPlainText())
        )
        self.diagnostics_tab_index = self.view_tabs.addTab(diagnostics_page, "传输诊断")
        diagnostics_action = view_menu.addAction("打开传输诊断页")
        diagnostics_action.triggered.connect(
            lambda: self.view_tabs.setCurrentIndex(self.diagnostics_tab_index)
        )

        self.yrange_spin = QtWidgets.QDoubleSpinBox(); self.yrange_spin.setValue(200)
        self.file_status = QtWidgets.QLabel("未打开文件")
        self.range_status = QtWidgets.QLabel("0.0–0.0 s")
        self.filter_status = QtWidgets.QLabel("5–50 Hz + 50/100 Hz harmonic notch")
        self.statusBar().addWidget(self.file_status, 1)
        self.statusBar().addPermanentWidget(self.range_status)
        self.statusBar().addPermanentWidget(self.filter_status)
        self.log_status = QtWidgets.QLabel("日志")
        self.log_status.setToolTip(str(APP_LOG_PATH))
        self.statusBar().addPermanentWidget(self.log_status)
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Left), self, activated=lambda: self.page(-1))
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Right), self, activated=lambda: self.page(1))

    def _add_tool_field(self, toolbar, label, widget, trailing=""):
        toolbar.addWidget(QtWidgets.QLabel(label + " "))
        toolbar.addWidget(widget)
        if trailing:
            toolbar.addWidget(QtWidgets.QLabel(trailing))

    def toggle_psd_dock(self, visible: bool):
        if hasattr(self, "psd_dock"):
            self.psd_dock.setVisible(bool(visible))

    def _filter_settings_changed(self, *_):
        hp, lp = float(self.hp_spin.value()), float(self.lp_spin.value())
        if hp >= lp:
            return
        self.sos_display_band = signal.butter(2, [hp, lp], btype="bandpass", fs=FS, output="sos")
        self.reset_processing_state()
        self.event_label.setVisible(self.filter_check.isChecked())
        self.update_fast_plots()

    def _total_samples(self):
        return (
            self.offline_uv.shape[1]
            if self.offline_uv is not None
            else int(self.ring.total_appended)
        )

    def _start_time_changed(self, value):
        if self.offline_uv is not None:
            self.offline_end = min(self._total_samples(), int((value + self.win_spin.value()) * FS))
            self.reset_psd_smoothing()
        self.update_fast_plots()

    def _window_changed(self, *_):
        if self.offline_uv is not None:
            self.offline_end = min(
                self._total_samples(),
                int((self.start_time_spin.value() + self.win_spin.value()) * FS),
            )
            self.reset_psd_smoothing()
        self.update_fast_plots()

    def _nav_region_changed(self, region=None):
        if getattr(self, "_syncing_nav", False):
            return
        active_region = region if region is not None else self.nav_region
        lo, hi = active_region.getRegion()
        self.win_spin.blockSignals(True); self.start_time_spin.blockSignals(True)
        self.win_spin.setValue(max(1, hi - lo)); self.start_time_spin.setValue(max(0, lo))
        self.win_spin.blockSignals(False); self.start_time_spin.blockSignals(False)
        if self.offline_uv is not None:
            self.offline_end = min(self._total_samples(), int(hi * FS))
            self.reset_psd_smoothing()
        self.update_fast_plots()

    def page(self, direction):
        total = self._total_samples() / FS
        width = self.win_spin.value()
        self.start_time_spin.setValue(max(0, min(max(0, total-width),
                                                  self.start_time_spin.value() + direction*width)))

    def eventFilter(self, obj, event):
        if (
            event.type() == QtCore.QEvent.Wheel
            and obj in getattr(self, "_scale_viewbox_channels", {})
        ):
            channel = self._scale_viewbox_channels[obj]
            if channel < 0:
                channel = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
            factor = 0.8 if event.angleDelta().y() > 0 else 1.25
            scale = self.channel_scales[channel]
            scale.setValue(
                float(np.clip(scale.value() * factor, scale.minimum(), scale.maximum()))
            )
            return True
        return super().eventFilter(obj, event)

    def _channel_scale_changed(self, channel: int, value: float):
        if (
            hasattr(self, "single_scale_spin")
            and int(channel) == int(self.single_channel_index)
        ):
            self.single_scale_spin.blockSignals(True)
            self.single_scale_spin.setValue(float(value))
            self.single_scale_spin.blockSignals(False)
        self.update_fast_plots()

    def _single_scale_changed(self, value: float):
        if not hasattr(self, "channel_scales"):
            return
        channel = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
        self.channel_scales[channel].setValue(float(value))

    def _plot_clicked(self, event):
        pos = self.stack_plot.getPlotItem().vb.mapSceneToView(event.scenePos())
        sensitivity = self.sensitivity_spin.value()
        ch = int(round((7.5 * sensitivity - pos.y()) / (2 * sensitivity)))
        if 0 <= ch < CHANNELS:
            self._select_channel(ch)

    def _wave_scene_clicked(self, event):
        """Select on one click; open the isolated channel tab on double-click."""
        if event.button() != QtCore.Qt.LeftButton:
            return
        scene_pos = event.scenePos()
        channel = None
        for ch, plot in enumerate(self.channel_plots):
            if plot.sceneBoundingRect().contains(scene_pos):
                channel = ch
                break
        if channel is None:
            return
        self._select_channel(channel)
        is_double = bool(event.double()) if hasattr(event, "double") else False
        if is_double:
            self.show_single_channel(channel)
            event.accept()

    def show_single_channel(self, channel: int):
        channel = int(np.clip(channel, 0, CHANNELS - 1))
        self.single_channel_index = channel
        self.single_channel_combo.blockSignals(True)
        self.single_channel_combo.setCurrentIndex(channel)
        self.single_channel_combo.blockSignals(False)
        self.single_scale_spin.blockSignals(True)
        self.single_scale_spin.setValue(self.channel_scales[channel].value())
        self.single_scale_spin.blockSignals(False)
        self.view_tabs.setCurrentIndex(self.single_tab_index)
        self.update_fast_plots()
        self.set_status(
            f"CH{channel+1} 已在“单通道放大”Tab 中独立显示；"
            "双击其他通道可直接切换。"
        )

    def _single_channel_changed(self, channel: int):
        if channel < 0:
            return
        self.single_channel_index = int(channel)
        self.single_scale_spin.blockSignals(True)
        self.single_scale_spin.setValue(self.channel_scales[self.single_channel_index].value())
        self.single_scale_spin.blockSignals(False)
        self._select_channel(self.single_channel_index)
        self.update_fast_plots()

    def _select_channel(self, ch):
        self.channel_combo.setCurrentIndex(ch)
        for i, curve in enumerate(self.stack_curves):
            curve.setPen(pg.mkPen(
                CHANNEL_COLORS[i],
                width=3.0 if i == ch else 2.0,
            ))
        self.single_curve.setPen(pg.mkPen(CHANNEL_COLORS[ch], width=2.4))
        if hasattr(self, "single_scale_spin"):
            self.single_scale_spin.blockSignals(True)
            self.single_scale_spin.setValue(self.channel_scales[ch].value())
            self.single_scale_spin.blockSignals(False)
        for i, button in enumerate(self.channel_buttons):
            button.setStyleSheet(
                "QToolButton{"
                f"background:{'#fff0e6' if i == ch else '#ffffff'};"
                "color:#2d2521;border:0;border-bottom:1px solid #e2e6eb;"
                "text-align:left;padding-left:10px;font-size:13px;"
                f"font-weight:{'bold' if i == ch else 'normal'};"
                "}"
            )

    def reference_is_srb2(self) -> bool:
        return self.reference_mode == REFERENCE_SRB2

    def impedance_series_default_kohm(self) -> float:
        return (
            LEAD_OFF_SERIES_SRB2_KOHM
            if self.reference_is_srb2()
            else LEAD_OFF_SERIES_SRB1_KOHM
        )

    def sync_impedance_series_compensation(self):
        if self.impedance_series_spin is None:
            return
        value = self.impedance_series_default_kohm()
        self.impedance_series_spin.setValue(value)
        self.impedance_series_spin.setToolTip(
            f"已按 {self.reference_short_name()} 参考自动设置为 {value:.2f} kΩ；"
            "也可按对应接口的外部短接实测值校准。"
        )

    def set_reference_mode_local(self, mode: int):
        self.reference_mode = REFERENCE_SRB2 if int(mode) == REFERENCE_SRB2 else REFERENCE_SRB1
        if hasattr(self, "reference_combo"):
            index = self.reference_combo.findData(self.reference_mode)
            if index >= 0:
                self.reference_combo.blockSignals(True)
                self.reference_combo.setCurrentIndex(index)
                self.reference_combo.blockSignals(False)
        self.sync_impedance_series_compensation()
        self.refresh_channel_parameter_labels()

    def reference_short_name(self) -> str:
        return "SRB2" if self.reference_is_srb2() else "SRB1"

    def bias_register_name(self) -> str:
        if self.current_mode == 0:
            return "BIAS_SENSP+BIAS_SENSN"
        return "BIAS_SENSN" if self.reference_is_srb2() else "BIAS_SENSP"

    @staticmethod
    def _decode_config_ack_packet(
        packet: bytes,
        expected_command: int,
        expected_argument: Optional[int] = None,
    ):
        packet = bytes(packet)
        if len(packet) != 12 or packet[0] != 0xBC:
            return None
        if packet[1] != (int(expected_command) & 0xFF):
            return None
        if expected_argument is not None and packet[2] != (int(expected_argument) & 0xFF):
            return None
        checksum = 0
        for value in packet[:11]:
            checksum ^= value
        if checksum != packet[11]:
            return None
        return {
            "command": packet[1],
            "argument": packet[2],
            "channel_register": packet[3],
            "bias_p": packet[4],
            "bias_n": packet[5],
            "misc1": packet[6],
            "loff_p": packet[4],
            "loff_n": packet[5],
            "loff_config": packet[6],
            "reference": packet[7],
            "mode": packet[8],
            "verified": bool(packet[9] & 0x01),
            "enabled_mask": packet[10],
        }

    def read_config_ack(
        self,
        expected_command: int,
        timeout: float = 1.8,
        expected_argument: Optional[int] = None,
    ):
        """Read one matching ADS register ACK with GATT-read fallback.

        V16 serial ACKs are drained from the dedicated reader queue while the
        normal serial parser is temporarily paused.  This removes the race where
        a Qt timer consumed a 12-byte configuration ACK as if it were EEG.
        """
        if not self.transport_connected():
            return None
        deadline = time.perf_counter() + timeout
        buffer = bytearray()
        marker = bytes((0xBC, expected_command & 0xFF))
        wanted_argument = (
            None if expected_argument is None else int(expected_argument) & 0xFF
        )
        next_direct_read = time.perf_counter() + 0.22
        serial_mode = self.active_transport == "serial"
        if serial_mode:
            self.serial_control_read_active = True
        try:
            while time.perf_counter() < deadline:
                chunk = b""
                if self.active_transport == "serial":
                    if self.serial_worker is not None:
                        chunk = self.serial_worker.drain_data(4096)
                elif self.active_transport == "ble" and self.ble_worker is not None:
                    try:
                        chunk = self.ble_worker.status_queue.get(timeout=0.02)
                    except queue.Empty:
                        chunk = b""
                    now = time.perf_counter()
                    if not chunk and now >= next_direct_read:
                        next_direct_read = now + 0.30
                        try:
                            direct = self.ble_worker.read_status_blocking(timeout=0.65)
                        except Exception:
                            direct = b""
                        parsed = self._decode_config_ack_packet(
                            direct, expected_command, wanted_argument
                        )
                        if parsed is not None:
                            return parsed

                if chunk:
                    buffer.extend(chunk)

                while True:
                    start = buffer.find(marker)
                    if start < 0:
                        if len(buffer) > 1:
                            del buffer[:-1]
                        break
                    if len(buffer) < start + 12:
                        if start:
                            del buffer[:start]
                        break
                    packet = bytes(buffer[start:start + 12])
                    del buffer[:start + 12]
                    parsed = self._decode_config_ack_packet(
                        packet, expected_command, wanted_argument
                    )
                    if parsed is not None:
                        return parsed

                if len(buffer) > 256:
                    del buffer[:-32]
                QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 5)
                time.sleep(0.004)
            return None
        finally:
            if serial_mode:
                self.serial_control_read_active = False

    def refresh_channel_parameter_labels(self):
        """Keep the per-channel hardware state visible without opening a dialog."""
        if not hasattr(self, "channel_buttons"):
            return
        for ch, button in enumerate(self.channel_buttons):
            name = self.channel_names[ch]
            enabled = bool(self.channel_enabled[ch])
            bias = "BIAS✓" if self.channel_bias[ch] else "BIAS—"
            power = "ON" if enabled else "OFF"
            reference = "SRB1全局"
            button.setText(f"{name}  {power}  ×{int(self.channel_gains[ch])}\n{bias}  {reference}")
            icon = QtGui.QPixmap(11, 11)
            icon.fill(QtGui.QColor("#56bd31" if enabled else "#8b969e"))
            button.setIcon(QtGui.QIcon(icon))
            button.setToolTip(
                f"{name} (INP{ch+1}): {'启用' if enabled else '禁用'}, "
                f"PGA ×{int(self.channel_gains[ch])}, "
                f"{'参与' if self.channel_bias[ch] else '不参与'} {self.bias_register_name()}；"
                + "EEG 模式使用全局 SRB1"
            )
            if hasattr(self, "channel_plots"):
                self.channel_plots[ch].setLabel("left", name, units="uV")
            if hasattr(self, "channel_combo"):
                self.channel_combo.setItemText(ch, name)
            if hasattr(self, "single_channel_combo"):
                self.single_channel_combo.setItemText(ch, name)

    def validated_channel_name(self, ch, name):
        name = str(name).strip()
        if not name:
            raise ValueError("通道名称不能为空。")
        if len(name) > 16:
            raise ValueError("通道名称最多 16 个字符。")
        if any(ord(char) < 32 or ord(char) > 126 for char in name):
            raise ValueError("通道名称只能使用英文、数字和 ASCII 符号。")
        duplicates = {
            existing.casefold()
            for index, existing in enumerate(self.channel_names)
            if index != ch
        }
        if name.casefold() in duplicates:
            raise ValueError("通道名称不能重复。")
        return name

    def open_channel_settings(self, ch):
        self._select_channel(ch)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"CH{ch+1} 通道设置")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
        channel_name = QtWidgets.QLineEdit(self.channel_names[ch])
        channel_name.setMaxLength(16)
        form.addRow("通道名称", channel_name)
        summary = QtWidgets.QLabel()
        summary.setStyleSheet("background:#fff0e6;color:#b83c00;border:1px solid #ffb589;padding:8px;font-weight:bold;")
        form.addRow(summary)
        enabled = QtWidgets.QCheckBox("启用该通道")
        enabled.setChecked(bool(self.channel_enabled[ch]))
        form.addRow("通道电源", enabled)
        gain = QtWidgets.QComboBox()
        gain.addItems([str(value) for value in VALID_GAINS])
        gain.setCurrentText(str(int(self.channel_gains[ch])))
        form.addRow("PGA 增益", gain)
        bias = QtWidgets.QCheckBox(f"加入 {self.bias_register_name()} 共模反馈计算")
        bias.setChecked(bool(self.channel_bias[ch]))
        form.addRow("BIAS", bias)
        note_text = (
            "V19 固定使用 SRB1：测量电极接 INxP，公共参考接 SRB1，"
            "MISC1.SRB1 在 EEG 模式中全局开启。"
        )
        note = QtWidgets.QLabel(note_text)
        note.setWordWrap(True)
        note.setStyleSheet("color:#5d6870;")
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        form.addRow(buttons)

        def update_summary(*_args):
            summary.setText(
                f"CH{ch+1}  |  {'ON' if enabled.isChecked() else 'OFF'}  |  "
                f"PGA ×{gain.currentText()}  |  BIAS {'YES' if bias.isChecked() else 'NO'}  |  "
                "SRB1 GLOBAL"
            )

        enabled.toggled.connect(update_summary)
        gain.currentTextChanged.connect(update_summary)
        bias.toggled.connect(update_summary)
        update_summary()
        selected_name = self.channel_names[ch]

        def accept_settings():
            nonlocal selected_name
            try:
                selected_name = self.validated_channel_name(ch, channel_name.text())
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(dialog, "通道名称无效", str(exc))
                return
            dialog.accept()

        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(accept_settings)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.apply_channel_settings(
            ch,
            enabled.isChecked(),
            int(gain.currentText()),
            bias.isChecked(),
            False,
            selected_name,
        )

    def apply_channel_settings(self, ch, enabled, gain, bias, srb2=None, channel_name=None):
        if channel_name is None:
            channel_name = self.channel_names[ch]
        channel_name = self.validated_channel_name(ch, channel_name)
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        srb2 = False
        effective_srb2 = False
        flags = (
            (0x01 if enabled else 0)
            | (0x02 if bias and enabled else 0)
            | (0x04 if effective_srb2 else 0)
        )
        was_streaming = bool(self.streaming)
        ack = None
        old_state = (
            bool(self.channel_enabled[ch]),
            int(self.channel_gains[ch]),
            bool(self.channel_bias[ch]),
            bool(self.channel_srb2[ch]),
        )
        try:
            if self.transport_connected() and self.offline_uv is None:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    if self.ble_worker is not None and self.active_transport == "ble":
                        self.ble_worker.set_streaming_hint(False)
                    time.sleep(0.12)
                # BLE configuration ACKs use STATUS, so never clear DATA here.
                # Clearing the reliable-delivery queue discarded valid tail EEG
                # and was the reason a channel toggle could create a fake host gap.
                if self.active_transport == "serial":
                    self.transport_reset_input_buffer()
                elif self.ble_worker is not None:
                    while True:
                        try:
                            self.ble_worker.status_queue.get_nowait()
                        except queue.Empty:
                            break
                if self.active_transport == "ble":
                    self.channel_enabled[ch] = bool(enabled)
                    self.channel_gains[ch] = int(gain)
                    self.channel_bias[ch] = bool(bias and enabled)
                    ack = self._ble_write_bulk_config(REFERENCE_SRB1)
                else:
                    self.transport_write(bytes([0xA7, ch & 0x07, gain & 0xFF, flags]))
                    ack = self.read_config_ack(0xA7, expected_argument=ch & 0x07)
                    if ack is None or ack["argument"] != (ch & 0x07) or not ack["verified"]:
                        raise RuntimeError(f"ADS1299 配置校验失败：CH{ch+1}")
            self.channel_enabled[ch] = bool(enabled)
            self.channel_gains[ch] = int(gain)
            self.channel_bias[ch] = bool(bias and enabled)
            self.channel_srb2[ch] = False
            self.channel_names[ch] = channel_name
            self.set_bias_checks(sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i]))
            self.refresh_channel_parameter_labels()
            # Start a fresh display/filter epoch for the new hardware channel
            # configuration, but do not discard already-received BLE DATA from
            # the transport queues. The raw BIN session remains continuous.
            self.ring.clear()
            self.reset_processing_state()
            self.last_seq = None
            self.first_seq = None
            self.first_clock = None
            readback = (
                f"；ADS读回 CHnSET=0x{(ack.get('channel_registers') or [ack.get('channel_register', 0xFF)] * 8)[ch]:02X}, "
                f"P=0x{ack['bias_p']:02X}, N=0x{ack['bias_n']:02X}"
                if ack is not None else "；仅更新离线显示参数"
            )
            self.set_status(
                f"已确认 CH{ch+1}: {'ON' if enabled else 'OFF'}, PGA×{gain}, "
                f"{self.bias_register_name()}={'YES' if bias and enabled else 'NO'}, "
                + "SRB1=GLOBAL"
                + readback
            )
        except Exception as exc:
            (
                self.channel_enabled[ch],
                self.channel_gains[ch],
                self.channel_bias[ch],
                self.channel_srb2[ch],
            ) = old_state
            self.refresh_channel_parameter_labels()
            QtWidgets.QMessageBox.critical(self, "通道设置失败", str(exc))
        finally:
            if was_streaming and self.transport_connected():
                try:
                    # Do not discard retained BLE tail blocks before restart.
                    if self.active_transport == "serial":
                        self.transport_reset_input_buffer()
                    self.transport_write(b"b")
                    self.streaming = True
                    if self.ble_worker is not None and self.active_transport == "ble":
                        self.ble_worker.set_streaming_hint(True)
                    self.last_seq = None
                    self.first_seq = None
                    self.first_clock = None
                except Exception:
                    self.streaming = False

    def apply_reference_mode(self):
        if self.active_transport == "ble" and not self.ble_supports_srb2:
            self.set_reference_mode_local(REFERENCE_SRB1)
            self.reference_combo.setCurrentIndex(max(0, self.reference_combo.findData(REFERENCE_SRB1)))
            self.set_status("当前 BLE 固件为固定 SRB1 版本，不能切换到 SRB2。")
            return
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        new_mode = int(self.reference_combo.currentData())
        was_streaming = bool(self.streaming)
        try:
            if self.active_transport == "ble" and self.transport_connected() and self.offline_uv is None:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    time.sleep(0.08)
                actual_reference, supports_srb2 = self.sync_ble_configuration(
                    requested_reference=new_mode, probe_capability=False
                )
                self.ble_supports_srb2 = bool(supports_srb2)
                self.set_reference_mode_local(actual_reference)
                self.set_bias_checks(
                    sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i])
                )
                self.ring.clear()
                self.filtered_ring.clear()
                self.reset_processing_state()
                if was_streaming:
                    self.transport_write(b"b")
                    self.streaming = True
                self.set_status(
                    f"BLE 参考已切换为 {self.reference_short_name()}；"
                    + (
                        "信号接 INxN，公共参考接 SRB2，BIAS 使用 SENSN。"
                        if actual_reference == REFERENCE_SRB2
                        else "信号接 INxP，公共参考接 SRB1，BIAS 使用 SENSP。"
                    )
                )
                return
            if self.transport_connected() and self.offline_uv is None:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    time.sleep(0.08)
                self.transport_reset_input_buffer()
                self.transport_write(bytes([0xA8, new_mode & 0x01]))
                time.sleep(0.12)

                # Re-send the eight channel configurations so a SRB2-capable
                # firmware receives the exact per-channel switch mask.
                payload = bytearray()
                for ch in range(CHANNELS):
                    enabled = bool(self.channel_enabled[ch])
                    flags = (
                        (0x01 if enabled else 0)
                        | (0x02 if self.channel_bias[ch] and enabled else 0)
                        | (
                            0x04
                            if new_mode == REFERENCE_SRB2
                            and self.channel_srb2[ch]
                            and enabled
                            else 0
                        )
                    )
                    payload.extend((0xA7, ch, int(self.channel_gains[ch]), flags))
                self.transport_write(payload)
                time.sleep(0.25)
                # A7-capable firmware returns one readback ACK per channel.
                # This bulk synchronization does not need to expose all eight
                # replies, so discard them before normal polling/streaming.
                self.transport_reset_input_buffer()
                if was_streaming:
                    self.transport_write(b"b")
                    self.streaming = True

            self.set_reference_mode_local(new_mode)
            self.set_bias_checks(
                sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i])
            )
            self.ring.clear()
            self.filtered_ring.clear()
            self.reset_processing_state()
            if new_mode == REFERENCE_SRB2:
                self.set_status(
                    "参考已切换为 SRB2：信号接 INxN，参考接 SRB2，"
                    "BIAS 自动使用 BIAS_SENSN；原始极性为 SRB2-INxN。"
                )
            else:
                self.set_status(
                    "参考已切换为 SRB1：信号接 INxP，参考接 SRB1，"
                    "BIAS 自动使用 BIAS_SENSP；原始极性为 INxP-SRB1。"
                )
        except Exception as exc:
            if was_streaming and self.transport_connected() and not self.streaming:
                try:
                    self.transport_reset_input_buffer()
                    self.transport_write(b"b")
                    self.streaming = True
                except Exception:
                    pass
            QtWidgets.QMessageBox.critical(self, "参考模式切换失败", str(exc))

    def _main_range_changed(self, _viewbox, x_range):
        if getattr(self, "_syncing_plot", False) or self.offline_uv is None:
            return
        width = max(1.0, float(x_range[1] - x_range[0]))
        self.win_spin.blockSignals(True)
        self.start_time_spin.blockSignals(True)
        self.win_spin.setValue(width)
        self.start_time_spin.setValue(max(0.0, float(x_range[0])))
        self.win_spin.blockSignals(False)
        self.start_time_spin.blockSignals(False)
        self.offline_end = min(self._total_samples(), int(x_range[1] * FS))
        self.reset_psd_smoothing()
        self.update_fast_plots()

    def export_csv(self):
        if self.offline_uv is None:
            QtWidgets.QMessageBox.information(self, "导出 CSV", "请先导入一个 BIN 或 BDF 文件。")
            return
        recordings_dir = RECORDINGS_DIR
        mne_dir = recordings_dir / "mne"
        mne_dir.mkdir(parents=True, exist_ok=True)
        source = Path(getattr(self, "loaded_path", self.raw_path or "ADS1299"))
        path = mne_dir / f"{source.stem}_mne.csv"
        header = "time_s," + ",".join(f"CH{i}_uV" for i in range(1, 9))
        matrix = np.column_stack((np.arange(self.offline_uv.shape[1]) / FS, self.offline_uv.T))
        np.savetxt(path, matrix, delimiter=",", header=header, comments="", fmt="%.7g")
        self.set_status(f"已导出 {path}")
        QtWidgets.QMessageBox.information(self, "导出完成", f"已生成：\n{path}")

    def export_biosignal_formats(self, source_path=None):
        """Interactively export the imported or just-recorded signal."""
        if isinstance(source_path, bool):
            source_path = None
        if self.streaming:
            QtWidgets.QMessageBox.information(
                self, "导出 BDF/FIF", "请先停止实时采集，再选择导出格式。"
            )
            return
        try:
            if source_path:
                self._load_bin_path(str(source_path))
            elif self.offline_uv is None:
                candidate = Path(self.raw_path) if self.raw_path else None
                if candidate and candidate.exists() and candidate.stat().st_size:
                    self._load_bin_path(str(candidate))
                else:
                    QtWidgets.QMessageBox.information(
                        self, "导出 BDF/FIF", "请先导入 BIN 或完成一次实时采集。"
                    )
                    return

            choices = ["BDF + MNE FIF", "BDF", "MNE FIF"]
            choice, ok = QtWidgets.QInputDialog.getItem(
                self,
                "选择导出格式",
                "请选择要生成的文件格式：",
                choices,
                0,
                False,
            )
            if not ok:
                return
            source = Path(getattr(self, "loaded_path", self.raw_path or "ADS1299.bin"))
            recordings_dir = RECORDINGS_DIR
            bdf_dir = recordings_dir / "bdf"
            fif_dir = recordings_dir / "fif"
            bdf_dir.mkdir(parents=True, exist_ok=True)
            fif_dir.mkdir(parents=True, exist_ok=True)
            stem = source.stem
            written = []
            if "BDF" in choice:
                bdf_path = bdf_dir / f"{stem}.bdf"
                self.save_bdf(bdf_path)
                written.append(bdf_path)
            if "FIF" in choice:
                fif_path = fif_dir / f"{stem}_raw.fif"
                self.build_mne_raw().save(
                    fif_path, overwrite=True, fmt="double", verbose="ERROR"
                )
                written.append(fif_path)
            names = "\n".join(str(path) for path in written)
            self.set_status(f"已导出：{', '.join(path.name for path in written)}")
            QtWidgets.QMessageBox.information(
                self, "导出完成", f"已生成：\n{names}"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "导出 BDF/FIF 失败", str(exc))

    def save_bdf(self, path: Path):
        """Write the unfiltered input-referred signal as 24-bit BDF+."""
        if self.offline_uv is None:
            raise RuntimeError("没有可导出的采样数据。")
        try:
            import pyedflib
        except ImportError as exc:
            raise RuntimeError(
                "缺少 BDF 依赖 pyedflib，请运行：py -3 -m pip install pyedflib"
            ) from exc

        path = Path(path)
        data = np.asarray(self.offline_uv, dtype=np.float64)
        writer = pyedflib.EdfWriter(
            str(path),
            CHANNELS,
            file_type=pyedflib.FILETYPE_BDFPLUS,
        )
        try:
            headers = []
            for ch in range(CHANNELS):
                finite = data[ch][np.isfinite(data[ch])]
                peak = float(np.max(np.abs(finite))) if finite.size else 1.0
                physical_peak = max(100, int(np.ceil(peak * 1.1)))
                headers.append({
                    "label": self.channel_names[ch],
                    "dimension": "uV",
                    "sample_frequency": FS,
                    "physical_min": -physical_peak,
                    "physical_max": physical_peak,
                    "digital_min": -8388608,
                    "digital_max": 8388607,
                    "transducer": "ADS1299",
                    "prefilter": "Raw, unfiltered",
                })
            writer.setSignalHeaders(headers)
            writer.setPatientCode("")
            writer.setEquipment("ADS1299")
            writer.writeSamples([
                np.nan_to_num(data[ch], nan=0.0, posinf=0.0, neginf=0.0)
                for ch in range(CHANNELS)
            ])
            remainder = data.shape[1] % FS
            if remainder:
                writer.writeAnnotation(
                    float(data.shape[1]) / FS,
                    float(FS - remainder) / FS,
                    "BDF_padding",
                )

            valid = np.asarray(self.offline_valid, dtype=bool)
            if valid.size == data.shape[1] and not valid.all():
                padded = np.r_[False, ~valid, False].astype(np.int8)
                edges = np.diff(padded)
                starts = np.flatnonzero(edges == 1)
                ends = np.flatnonzero(edges == -1)
                for start, end in zip(starts, ends):
                    writer.writeAnnotation(
                        float(start) / FS,
                        float(end - start) / FS,
                        "BAD_frame",
                    )
        finally:
            writer.close()

    def _write_bdf_data(
        self,
        path: Path,
        data: np.ndarray,
        valid: np.ndarray,
        *,
        markers: tuple[MarkerEvent, ...] = tuple(),
        recording_started_at: float | None = None,
        first_sequence: int | None = None,
        overwrite: bool = False,
    ) -> None:
        if not overwrite and Path(path).exists():
            raise FileExistsError(f"BDF output already exists: {path}")
        data = np.asarray(data, dtype=np.float64)
        valid = np.asarray(valid, dtype=bool)
        if data.ndim != 2 or data.shape[0] != CHANNELS or data.shape[1] <= 0:
            raise ValueError("BDF data must have shape (8, samples)")
        if valid.ndim != 1 or valid.size != data.shape[1]:
            raise ValueError("BDF validity length must match data")
        if markers and recording_started_at is None:
            raise ValueError("recording_started_at is required for marker export")
        try:
            import pyedflib
        except ImportError as exc:
            raise RuntimeError(
                "缺少 BDF 依赖 pyedflib，请运行：uv sync --extra export"
            ) from exc

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = pyedflib.EdfWriter(
            str(path),
            CHANNELS,
            file_type=pyedflib.FILETYPE_BDFPLUS,
        )
        try:
            headers = []
            for ch in range(CHANNELS):
                finite = data[ch][np.isfinite(data[ch])]
                peak = float(np.max(np.abs(finite))) if finite.size else 1.0
                physical_peak = max(100, int(np.ceil(peak * 1.1)))
                headers.append({
                    "label": self.channel_names[ch],
                    "dimension": "uV",
                    "sample_frequency": FS,
                    "physical_min": -physical_peak,
                    "physical_max": physical_peak,
                    "digital_min": -8388608,
                    "digital_max": 8388607,
                    "transducer": "ADS1299",
                    "prefilter": "Raw, unfiltered",
                })
            writer.setSignalHeaders(headers)
            writer.setPatientCode("")
            writer.setEquipment("ADS1299")
            writer.writeSamples([
                np.nan_to_num(data[ch], nan=0.0, posinf=0.0, neginf=0.0)
                for ch in range(CHANNELS)
            ])
            annotations = []
            remainder = data.shape[1] % FS
            # A duration annotation covering the padded tail can hide later
            # user markers in some BDF+ readers. The writer still performs
            # the required record padding; keep this legacy annotation only
            # for exports without user events.
            if remainder and not markers:
                annotations.append(
                    (
                        float(data.shape[1]) / FS,
                        float(FS - remainder) / FS,
                        "BDF_padding",
                    )
                )

            if not valid.all():
                padded = np.r_[False, ~valid, False].astype(np.int8)
                edges = np.diff(padded)
                starts = np.flatnonzero(edges == 1)
                ends = np.flatnonzero(edges == -1)
                for start, end in zip(starts, ends):
                    annotations.append(
                        (
                            float(start) / FS,
                            float(end - start) / FS,
                            "BAD_frame",
                        )
                    )
            for marker in markers:
                onset, duration, text = bdf_annotation_for_marker(
                    marker,
                    recording_started_at=float(recording_started_at),
                    first_sequence=first_sequence,
                    sample_rate=FS,
                    sample_count=int(data.shape[1]),
                )
                annotations.append((onset, duration, text))
            for onset, duration, text in sorted(annotations, key=lambda item: item[0]):
                writer.writeAnnotation(onset, duration, text)
        finally:
            writer.close()

    def export_recording_bdf(
        self,
        path: Path,
        markers: tuple[MarkerEvent, ...],
        *,
        recording_id: str,
        recording_started_at: float,
        first_sequence: int | None,
        overwrite: bool = False,
    ) -> dict:
        """Export every completed BIN segment from the last live recording."""

        if not isinstance(recording_id, str) or not recording_id:
            raise ValueError("recording_id must not be empty")
        path = Path(path)
        snapshot = self.raw_writer.snapshot()
        segment_records = snapshot.get("segments", [])
        segment_paths = [
            Path(record["path"])
            for record in segment_records
            if isinstance(record, dict) and isinstance(record.get("path"), str)
        ]
        if not segment_paths:
            first_path = snapshot.get("first_path")
            if isinstance(first_path, str) and first_path:
                segment_paths = [Path(first_path)]
        segment_paths = [segment for segment in segment_paths if segment.exists()]
        if not segment_paths:
            raise RuntimeError("no_completed_recording")

        parser = AdsFrameParser(self.channel_lsb_uv)
        frames: list[Frame] = []
        for segment_path in segment_paths:
            frames.extend(parser.feed(segment_path.read_bytes()))
        if not frames:
            raise RuntimeError("recording_contains_no_valid_frames")

        (
            data,
            valid,
            sequence,
            _modes,
            _lost,
            _filled,
            _gap_events,
            _large_discontinuities,
            _last_sequence,
            _last_mode,
        ) = expand_frames_to_timeline(
            frames,
            previous_sequence=None,
            previous_mode=int(frames[0].mode),
        )
        if first_sequence is None:
            first_sequence = int(sequence[0])
        for marker in markers:
            if marker.recording_id != recording_id:
                raise ValueError("marker recording_id does not match export")
        self._write_bdf_data(
            path,
            data,
            valid,
            markers=tuple(markers),
            recording_started_at=recording_started_at,
            first_sequence=first_sequence,
            overwrite=overwrite,
        )
        return {
            "path": str(path.resolve()),
            "recording_id": recording_id,
            "event_count": len(markers),
            "sample_count": int(data.shape[1]),
        }

    def build_mne_raw(self):
        """Build an unfiltered MNE RawArray from the currently imported file."""
        if self.offline_uv is None:
            raise RuntimeError("请先导入一个 BIN 或 BDF 文件。")

        import mne

        channel_names = [f"CH{i}" for i in range(1, CHANNELS + 1)]
        info = mne.create_info(
            channel_names, FS, ch_types=[MNE_CHANNEL_TYPE] * CHANNELS
        )
        info["line_freq"] = 50.0
        info["description"] = (
            f"ADS1299 GUI file import: "
            f"{Path(getattr(self, 'loaded_path', 'unknown')).name}"
        )
        raw = mne.io.RawArray(
            self.offline_uv.astype(np.float64) * 1e-6,
            info,
            verbose="ERROR",
        )

        # Preserve CRC/validity information as standard MNE BAD annotations.
        valid = np.asarray(self.offline_valid, dtype=bool)
        if valid.size == self.offline_uv.shape[1] and not valid.all():
            bad = ~valid
            padded = np.r_[False, bad, False].astype(np.int8)
            edges = np.diff(padded)
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            annotations = mne.Annotations(
                onset=starts.astype(float) / FS,
                duration=(ends - starts).astype(float) / FS,
                description=["BAD_frame"] * len(starts),
            )
            raw.set_annotations(annotations)
        return raw

    def save_mne_exports(self) -> Tuple[Path, Path]:
        """Save automatic MNE interchange CSV and native FIF exports."""
        if self.offline_uv is None:
            raise RuntimeError("请先导入一个 BIN 或 BDF 文件。")

        recordings_dir = RECORDINGS_DIR
        mne_dir = recordings_dir / "mne"
        fif_dir = recordings_dir / "fif"
        mne_dir.mkdir(parents=True, exist_ok=True)
        fif_dir.mkdir(parents=True, exist_ok=True)

        source_stem = Path(getattr(self, "loaded_path", "ADS1299")).stem
        mne_csv_path = mne_dir / f"{source_stem}_mne.csv"
        fif_path = fif_dir / f"{source_stem}_raw.fif"

        # MNE stores EEG in volts, so this interchange CSV deliberately uses V.
        time_s = np.arange(self.offline_uv.shape[1], dtype=np.float64) / FS
        matrix = np.column_stack(
            (time_s, self.offline_uv.astype(np.float64).T * 1e-6)
        )
        header = "time_s," + ",".join(
            f"CH{i}_V" for i in range(1, CHANNELS + 1)
        )
        np.savetxt(
            mne_csv_path,
            matrix,
            delimiter=",",
            header=header,
            comments="",
            fmt="%.10g",
        )

        raw = self.build_mne_raw()
        raw.save(fif_path, overwrite=True, fmt="double", verbose="ERROR")
        return mne_csv_path, fif_path

    def open_mne_browser(self):
        if self.offline_uv is None:
            QtWidgets.QMessageBox.information(self, "MNE 浏览器", "请先导入一个 BIN 或 BDF 文件。")
            return
        try:
            self.build_mne_raw().plot(
                duration=self.win_spin.value(), scalings={"eeg": self.sensitivity_spin.value()*1e-6},
                block=False, title=Path(getattr(self, "loaded_path", "ADS1299")).name)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "MNE 浏览器", str(exc))

    # ---------------- helpers ----------------
    def calc_lsb_uv(self, gain: Optional[float] = None) -> float:
        actual_gain = float(self.gain if gain is None else gain)
        return VREF / (actual_gain * (2**23 - 1)) * 1e6

    def channel_lsb_uv(self) -> np.ndarray:
        return VREF / (self.channel_gains.astype(float) * (2**23 - 1)) * 1e6

    def adc_saturation_limits_uv(self) -> np.ndarray:
        """Per-channel input-referred rail guard in microvolts."""
        return (
            ADC_SATURATION_FRACTION
            * (2**23 - 1)
            * np.asarray(self.channel_lsb_uv(), dtype=float)
        )

    def saturation_mask_uv(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=float)
        if values.ndim != 2 or values.shape[0] != CHANNELS:
            return np.zeros_like(values, dtype=bool)
        limits = self.adc_saturation_limits_uv()[:, None]
        return np.isfinite(values) & (np.abs(values) >= limits)

    def set_status(self, text: str):
        self.status_label.setText(text)
        if text != getattr(self, "_last_logged_status", None):
            APP_LOGGER.info("status: %s", text)
            self._last_logged_status = text

    def open_log_directory(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        APP_LOGGER.info("opening log directory: %s", LOG_DIR)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(LOG_DIR)))

    def selected_transport(self) -> str:
        if hasattr(self, "transport_combo"):
            return str(self.transport_combo.currentData() or "serial")
        return "serial"

    def transport_connected(self) -> bool:
        if self.active_transport == "serial":
            return bool(self.ser and self.ser.is_open)
        if self.active_transport == "ble":
            return bool(self.ble_connected)
        return False

    def transport_description(self) -> str:
        if self.active_transport == "serial" and self.ser and self.ser.is_open:
            return f"USB {self.ser.port}"
        if self.active_transport == "ble" and self.ble_connected:
            return f"BLE {self.ble_device_name or self.ble_device_address}"
        return "未连接"

    def transport_mode_changed(self):
        if self.transport_connected() or self.transport_connecting:
            return
        kind = self.selected_transport()
        self.port_combo.clear()
        if kind == "ble":
            self.serial_label.setText("蓝牙")
            self.refresh_btn.setText("扫描蓝牙")
            self.connect_btn.setText("连接蓝牙")
            self.reference_combo.setEnabled(False)
            self.apply_reference_btn.setEnabled(False)
            self.reference_combo.setToolTip(
                "V19 固定使用 SRB1。"
            )
        else:
            self.serial_label.setText("串口")
            self.refresh_btn.setText("扫描串口")
            self.connect_btn.setText("打开串口")
            self.reference_combo.setEnabled(False)
            self.apply_reference_btn.setEnabled(False)
            self.reference_combo.setToolTip(
                "新版本固定使用 SRB1：每通道信号接 INxP，公共参考接 SRB1。"
            )
        self._apply_transport_timing(kind)
        self.refresh_ports()

    def _apply_transport_timing(self, kind: Optional[str] = None):
        """Apply the scheduler proven for each transport instead of one compromise."""
        selected = str(kind or self.active_transport or self.selected_transport() or "serial")
        poll_ms = BLE_POLL_INTERVAL_MS if selected == "ble" else SERIAL_POLL_INTERVAL_MS
        plot_ms = BLE_PLOT_INTERVAL_MS if selected == "ble" else SERIAL_PLOT_INTERVAL_MS
        if hasattr(self, "serial_timer"):
            self.serial_timer.setInterval(int(poll_ms))
        if hasattr(self, "plot_timer"):
            self.plot_timer.setInterval(int(plot_ms))
    def refresh_ports(self):
        if self.transport_connected() or self.transport_connecting:
            return
        if self.selected_transport() == "ble":
            self.port_combo.clear()
            self.port_combo.addItem("正在扫描 BLE…", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            if not BLE_AVAILABLE or self.ble_worker is None:
                self.set_status(f"BLE 不可用：请运行 install_and_run.bat 安装 bleak。{BLE_IMPORT_ERROR}")
                return
            self.ble_worker.scan(5.0)
            return

        current_device = self.port_combo.currentData()
        self.port_combo.clear()
        self.port_device_map = {}
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        if not ports:
            self.port_combo.addItem("未发现串口", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.set_status("未发现串口：请插入设备后点击“扫描串口”。")
        else:
            for info in ports:
                description = (info.description or info.manufacturer or "未知设备").strip()
                label = f"{info.device} — {description}"
                self.port_combo.addItem(label, userData=info.device)
                self.port_device_map[label] = info.device
            self.port_combo.setEnabled(True)
            self.connect_btn.setEnabled(True)
            if current_device:
                index = self.port_combo.findData(current_device)
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
            if len(ports) == 1:
                self.set_status(f"发现串口 {ports[0].device}：确认设备后点击“打开串口”。")
            else:
                self.set_status(f"发现 {len(ports)} 个串口：请选择正确设备后点击“打开串口”。")

    def on_ble_scan_started(self):
        self.set_status("正在扫描 BLE，约 5 秒…")

    def on_ble_scan_finished(self, rows):
        if self.selected_transport() != "ble" or self.transport_connected():
            return
        self.port_combo.clear()
        if not rows:
            self.port_combo.addItem("未发现 BLE 设备", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.set_status("未发现 BLE：确认固件已烧录、板子已上电且未被其他软件连接。")
            return
        preferred_index = -1
        for row in rows:
            label = f"{row['name']} — {row['address']}"
            self.port_combo.addItem(label, userData=row["key"])
            if row.get("preferred") and preferred_index < 0:
                preferred_index = self.port_combo.count() - 1
        if preferred_index >= 0:
            self.port_combo.setCurrentIndex(preferred_index)
            self.set_status("已发现兼容的 OmniBCI BLE 设备，点击“连接蓝牙”。")
        else:
            self.set_status("未看到已知的 OmniBCI 设备名；仍可选择设备，连接后会校验 GATT 与固件协议。")
        self.port_combo.setEnabled(True)
        self.connect_btn.setEnabled(True)

    def toggle_connection(self):
        if self.transport_connected() or self.transport_connecting:
            self.disconnect_transport()
            return
        if self.selected_transport() == "ble":
            key = self.port_combo.currentData()
            if not key:
                QtWidgets.QMessageBox.warning(self, "BLE", "请先扫描蓝牙并选择 OmniBCI-C3-SRB1-V3、OmniBCI-C3-SRB2 或兼容设备。")
                return
            if self.ble_worker is None:
                QtWidgets.QMessageBox.critical(self, "BLE", "缺少 bleak，请运行 install_and_run.bat。")
                return
            self.transport_connecting = True
            self.transport_combo.setEnabled(False)
            self.port_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
            self.connect_btn.setText("连接中…")
            self.connect_btn.setEnabled(False)
            self.ble_worker.connect_device(str(key))
            return

        port = self.port_combo.currentData()
        if not port:
            QtWidgets.QMessageBox.warning(self, "串口", "请先点击“扫描串口”，并选择一个设备。")
            return
        try:
            self.ser = serial.Serial(
                port,
                BAUD,
                timeout=SERIAL_READER_TIMEOUT_S,
                write_timeout=0.5,
            )
            self.serial_buffer_configured = False
            self.serial_buffer_error = ""
            try:
                # Best-effort Windows driver buffer.  Unlike V15, failure is
                # visible in diagnostics instead of silently ignored.
                self.ser.set_buffer_size(rx_size=SERIAL_RX_BUFFER_BYTES, tx_size=65536)
                self.serial_buffer_configured = True
            except Exception as buffer_exc:
                self.serial_buffer_error = str(buffer_exc)
            self.serial_worker = SerialTransportWorker(self.ser)
            self.serial_worker.buffer_configured = self.serial_buffer_configured
            self.serial_worker.buffer_error = self.serial_buffer_error
            self.serial_worker.start()
            self.active_transport = "serial"
            self._apply_transport_timing("serial")
            QtWidgets.QApplication.processEvents()
            time.sleep(0.7)
            self.transport_reset_input_buffer()
            self.transport_write(b"s")
            self.connect_btn.setText("关闭串口")
            self.transport_combo.setEnabled(False)
            self.port_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
            self.apply_reference_mode()
            self.set_status(
                f"已打开 {port}，并同步 {self.reference_short_name()} 参考与通道参数。"
                "现在可以点击“开始采集”。"
            )
        except Exception as exc:
            self.ser = None
            self.active_transport = None
            self.transport_combo.setEnabled(True)
            QtWidgets.QMessageBox.critical(self, "连接失败", str(exc))

    def on_ble_connecting(self, _key: str):
        self.set_status("正在连接并订阅 DATA/STATUS 特征…")

    @staticmethod
    def ble_reference_hint_from_name(name: str):
        normalized = str(name or "").strip().upper()
        if "SRB2" in normalized:
            return REFERENCE_SRB2
        if "SRB1" in normalized:
            return REFERENCE_SRB1
        return None

    def _ble_write_channel_config(self, ch: int, reference_mode: int):
        """V19 applies one atomic SRB1-only snapshot for every channel edit."""
        return self._ble_write_bulk_config(REFERENCE_SRB1)

    def _ble_write_bulk_config(self, reference_mode: int):
        """Configure and read back all ADS1299 registers in one V1 transaction."""
        if self.ble_worker is None:
            raise RuntimeError("BLE 后台线程未就绪")
        enabled_mask = sum(
            (1 << ch) for ch in range(CHANNELS) if self.channel_enabled[ch]
        ) & 0xFF
        bias_mask = sum(
            (1 << ch)
            for ch in range(CHANNELS)
            if self.channel_enabled[ch] and self.channel_bias[ch]
        ) & 0xFF
        payload = encode_set_config(
            self.current_mode,
            enabled_mask,
            bias_mask,
            self.impedance_mask if self.impedance_active else 0,
            self.channel_gains,
        )
        snapshot = decode_config_snapshot(
            self.ble_worker.request_blocking(MSG_SET_CONFIG, payload, timeout=4.0)
        )
        if not snapshot.verified or snapshot.enabled_mask != enabled_mask:
            raise RuntimeError("ADS1299 配置读回不一致")
        self.ble_worker.config_snapshot = snapshot
        return {
            "verified": snapshot.verified,
            "enabled_mask": snapshot.enabled_mask,
            "bias_p": snapshot.bias_p,
            "bias_n": snapshot.bias_n,
            "reference": REFERENCE_SRB1,
            "generation": snapshot.generation,
            "channel_registers": snapshot.channel_registers,
        }

    def sync_ble_configuration(self, requested_reference=None, probe_capability: bool = True):
        """V19 is fixed SRB1; connection handshake already read device state."""
        self.set_reference_mode_local(REFERENCE_SRB1)
        return REFERENCE_SRB1, False

    def apply_ble_config_snapshot(self, snapshot):
        gain_by_code = {0: 1, 1: 2, 2: 4, 3: 6, 4: 8, 5: 12, 6: 24}
        for ch, register in enumerate(snapshot.channel_registers):
            self.channel_enabled[ch] = not bool(register & 0x80)
            self.channel_gains[ch] = gain_by_code.get((register >> 4) & 0x07, 24)
            self.channel_bias[ch] = bool(snapshot.bias_p & (1 << ch))
            self.channel_srb2[ch] = False
        self.current_mode = int(snapshot.mode)
        self.set_reference_mode_local(REFERENCE_SRB1)
        self.set_bias_checks(int(snapshot.bias_p))

    def on_ble_connected(self, name: str, address: str, mtu: int, reconnected: bool):
        self.transport_connecting = False
        self.active_transport = "ble"
        self._apply_transport_timing("ble")
        self.ble_connected = True
        self.ble_device_name = name
        self.ble_device_address = address
        self.ble_peer_mtu = int(mtu)
        self.ble_low_mtu_warned = False
        self.ble_protocol_warned = False
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("断开蓝牙")
        self.transport_combo.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.reference_combo.setEnabled(False)
        self.apply_reference_btn.setEnabled(False)

        # During an active recording the ESP32 keeps sampling and retains BLE
        # blocks across a radio disconnect.  Do not send s/A5/b here: doing so
        # destroys the retained session and creates a real sequence gap exactly
        # when the automatic reconnect was supposed to recover it.
        if reconnected and self.streaming:
            if self.ble_worker is not None:
                self.ble_worker.set_streaming_hint(True)
            self.set_status(
                f"BLE 已自动重连并完成 V19/V1 握手：{name}，MTU={mtu}；"
                "正在继续原可靠会话并补传断线期间数据。"
            )
            return

        try:
            self.ble_supports_srb2 = False
            self.ble_reference_profile = "srb1_fixed"
            snapshot = self.ble_worker.config_snapshot if self.ble_worker is not None else None
            if snapshot is None:
                raise RuntimeError("未收到 ADS1299 寄存器快照")
            self.apply_ble_config_snapshot(snapshot)
            self.refresh_channel_parameter_labels()
            self.mode_before_internal_short = self.current_mode if self.current_mode in (0, 1, 2) else 1
            self.mode_combo.setCurrentIndex(self._mode_index_from_code(self.current_mode))
            self._sync_internal_short_button(self.current_mode == 3)
            action = "已自动重连" if reconnected else "已连接"
            info = self.ble_worker.device_info if self.ble_worker is not None else None
            firmware = info.get("firmware", (19, 0, 0)) if info else (19, 0, 0)
            protocol = info.get("protocol", 1) if info else 1
            self.set_status(
                f"BLE {action}并确认设备就绪：{name}，MTU={mtu}，"
                f"固件 V{firmware[0]}.{firmware[1]}.{firmware[2]}，协议 V{protocol}，固定 SRB1。"
                + ("采集已恢复。" if reconnected and self.streaming else "点击“开始采集”。")
            )
        except Exception as exc:
            self.set_status(f"BLE 握手后初始化界面失败：{exc}")

    def on_ble_disconnected(self, reason: str, will_reconnect: bool):
        self.ble_connected = False
        self.transport_connecting = bool(will_reconnect)
        if will_reconnect:
            self.connect_btn.setText("自动重连中…")
            self.connect_btn.setEnabled(True)
            self.set_status(f"{reason}；后台正在自动重连。")
            return
        self.active_transport = None
        self.transport_connecting = False
        self.streaming = False
        self.close_raw_file()
        self.ble_rx_buffer.clear()
        self.ble_batch_started_monotonic = None
        if self.ble_worker is not None:
            self.ble_worker.clear_data()
        self.connect_btn.setText("连接蓝牙")
        self.connect_btn.setEnabled(True)
        self.transport_combo.setEnabled(True)
        self.port_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.set_status(reason)

    def on_ble_data(self, payload):
        if self.active_transport != "ble":
            return
        data = bytes(payload)
        if data:
            self.ble_rx_buffer.extend(data)

    def on_ble_status(self, payload):
        data = bytes(payload)
        if len(data) < 32 or data[:2] != b"\xBC\x53":
            return
        flags = data[5]
        status = {
            "status_protocol": data[2],
            "phase": data[3],
            "mode": data[4],
            "flags": flags,
            "mtu": int.from_bytes(data[6:8], "little"),
            "sequence": int.from_bytes(data[8:12], "little"),
            "queue_drop": int.from_bytes(data[12:16], "little"),
            "notify_error": int.from_bytes(data[16:20], "little"),
            "command_drop": int.from_bytes(data[20:24], "little"),
            "mtu_blocked": int.from_bytes(data[24:28], "little"),
            "blocks_sent": int.from_bytes(data[28:32], "little"),
        }
        if len(data) >= 72:
            status.update({
                "reliable_stored": int.from_bytes(data[32:34], "little"),
                "reliable_outstanding": int.from_bytes(data[34:36], "little"),
                "reliable_highest_acked": int.from_bytes(data[36:40], "little"),
                "reliable_next_block": int.from_bytes(data[40:44], "little"),
                "reliable_ack_count": int.from_bytes(data[44:48], "little"),
                "reliable_nack_count": int.from_bytes(data[48:52], "little"),
                "reliable_retransmit": int.from_bytes(data[52:56], "little"),
                "reliable_recovered": int.from_bytes(data[56:60], "little"),
                "reliable_overflow": int.from_bytes(data[60:64], "little"),
                "reliable_unknown_nack": int.from_bytes(data[64:68], "little"),
                "reliable_protocol_error": int.from_bytes(data[68:72], "little"),
            })
        if len(data) >= 76:
            status["config_generation"] = int.from_bytes(data[72:76], "little")

        previous = dict(self.ble_status)
        counter_keys = (
            "queue_drop", "notify_error", "command_drop", "mtu_blocked", "blocks_sent",
            "reliable_ack_count", "reliable_nack_count", "reliable_retransmit",
            "reliable_recovered", "reliable_overflow", "reliable_unknown_nack",
            "reliable_protocol_error",
        )
        delta = {}
        for key in counter_keys:
            if key not in status:
                continue
            current = int(status.get(key, 0))
            if key not in previous:
                delta[key] = 0
                continue
            old = int(previous.get(key, 0))
            # Counter reset/reboot is treated as a fresh baseline, not as a huge
            # unsigned wrap. Genuine uint32 wrap is irrelevant at EEG timescales.
            delta[key] = current - old if current >= old else current
        self.ble_status_delta = delta
        self.ble_status = status
        self.ble_peer_mtu = self.ble_status["mtu"]
        self.current_mode = int(self.ble_status["mode"])
        self._sync_internal_short_button(self.current_mode == 3)
        if (len(data) < 76 or data[2] != 0x04) and not self.ble_protocol_warned:
            self.ble_protocol_warned = True
            self.set_status(
                "BLE STATUS 格式不匹配：请烧录 SRB1-only 固件 V19。"
            )
        if self.ble_peer_mtu >= BLE_MIN_STREAM_MTU:
            self.ble_low_mtu_warned = False
        if self.ble_peer_mtu < BLE_MIN_STREAM_MTU and not self.ble_low_mtu_warned:
            self.ble_low_mtu_warned = True
            self.set_status(
                f"BLE 已连接但 MTU={self.ble_peer_mtu}<100，固件会阻止 EEG Notify。"
                "请关闭其他蓝牙软件、重新连接或更新电脑蓝牙驱动。"
            )

    def on_ble_info(self, text: str):
        if self.active_transport == "ble" or self.transport_connecting:
            self.set_status(text)

    def on_ble_error(self, text: str):
        self.transport_connecting = False
        if self.active_transport != "ble":
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("连接蓝牙")
        self.set_status(text)
        QtWidgets.QMessageBox.warning(self, "BLE", text)

    def disconnect_transport(self):
        if self.active_transport == "serial":
            self.stop_stream()
            if self.serial_worker is not None:
                try:
                    self.serial_worker.stop(timeout=2.0, close_port=False)
                except Exception:
                    pass
                self.serial_worker = None
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.active_transport = None
            self.connect_btn.setText("打开串口")
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.set_status("串口已关闭。")
            return
        if self.active_transport == "ble" or self.transport_connecting:
            self.stop_stream()
            self.transport_connecting = False
            if self.ble_worker is not None:
                try:
                    self.ble_worker.disconnect_blocking()
                except Exception as exc:
                    self.set_status(f"BLE 断开异常：{exc}")
            self.ble_connected = False
            self.active_transport = None
            self.connect_btn.setText("连接蓝牙")
            self.connect_btn.setEnabled(True)
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.set_status("BLE 已断开。")

    def disconnect_serial(self):
        self.disconnect_transport()

    def require_transport(self) -> bool:
        if not self.transport_connected():
            kind = "蓝牙" if self.selected_transport() == "ble" else "串口"
            QtWidgets.QMessageBox.warning(self, "设备未连接", f"请先扫描并连接{kind}设备。")
            return False
        return True

    def transport_write(self, data: bytes):
        payload = bytes(data)
        if self.active_transport == "serial":
            if not self.ser or not self.ser.is_open:
                raise RuntimeError("串口未连接")
            return self.ser.write(payload)
        if self.active_transport == "ble":
            if not self.ble_connected or self.ble_worker is None:
                raise RuntimeError("BLE 未连接")
            self.ble_worker.write_blocking(payload)
            return len(payload)
        raise RuntimeError("设备未连接")

    def transport_reset_input_buffer(
        self, clear_status: bool = True, reset_reliable: bool = False
    ):
        if self.active_transport == "serial":
            if self.serial_worker is not None:
                self.serial_worker.clear_data(clear_driver=True)
            elif self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
        elif self.active_transport == "ble":
            self.ble_rx_buffer.clear()
            self.ble_batch_started_monotonic = None
            if self.ble_worker is not None:
                self.ble_worker.clear_data(reset_reliable=reset_reliable)
            if clear_status and self.ble_worker is not None:
                while True:
                    try:
                        self.ble_worker.status_queue.get_nowait()
                    except queue.Empty:
                        break
        self.last_serial_waiting_bytes = 0

    def _run_api_on_gui(self, operation: str, payload: dict) -> dict:
        """Run a control operation on the Qt thread and return its result."""

        request = {
            "operation": operation,
            "payload": dict(payload),
            "done": threading.Event(),
            "result": None,
            "error": None,
        }
        self.api_gui_request.emit(request)
        if not request["done"].wait(timeout=120.0):
            raise RuntimeError("gui_control_timeout")
        if request["error"] is not None:
            raise request["error"]
        result = request["result"]
        if not isinstance(result, dict):
            raise RuntimeError("GUI control handler returned an invalid result")
        return result

    @QtCore.Slot(object)
    def _handle_api_gui_request(self, request: dict) -> None:
        try:
            operation = request["operation"]
            payload = request["payload"]
            if operation == "stop_measurement":
                self.stop_stream(offer_export=False)
                request["result"] = {
                    "recording_id": payload["recording_id"],
                    "stopped": True,
                }
            elif operation == "export_bdf":
                request["result"] = self.export_recording_bdf(
                    Path(payload["path"]),
                    payload["markers"],
                    recording_id=payload["recording_id"],
                    recording_started_at=payload["recording_started_at"],
                    first_sequence=payload["first_sequence"],
                    overwrite=payload["overwrite"],
                )
            else:
                raise RuntimeError("unsupported_gui_control")
        except BaseException as exc:
            request["error"] = exc
        finally:
            request["done"].set()

    def _api_stop_measurement(self) -> dict:
        if not self.streaming or not self.recording_session_id:
            raise RuntimeError("not_recording")
        recording_id = self.recording_session_id
        return self._run_api_on_gui(
            "stop_measurement", {"recording_id": recording_id}
        )

    def _api_export_bdf(self, request: dict, markers: tuple[MarkerEvent, ...]) -> dict:
        path = request.get("path")
        overwrite = request.get("overwrite", False)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a bool")
        if self.stream_server is None:
            raise RuntimeError("stream_api_unavailable")
        state = self.stream_server.recording_snapshot()
        recording_id = state.get("recording_id")
        if not isinstance(recording_id, str) or not recording_id:
            raise RuntimeError("no_recording")
        return self._run_api_on_gui(
            "export_bdf",
            {
                "path": path,
                "overwrite": overwrite,
                "markers": tuple(markers),
                "recording_id": recording_id,
                "recording_started_at": state["recording_started_at"],
                "first_sequence": state["first_sequence"],
            },
        )

    def _recording_folder(self) -> Path:
        folder = RECORDINGS_DIR / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _recording_configuration_snapshot(self) -> dict:
        reference = "SRB2" if self.reference_is_srb2() else "SRB1"
        transport = self.transport_description() if self.transport_connected() else "disconnected"
        return {
            "sample_rate_hz": FS,
            "frame_bytes": FRAME_BYTES,
            "transport": transport,
            "ble_device_name": self.ble_device_name or "",
            "ble_device_address": self.ble_device_address or "",
            "reference": reference,
            "reference_code": int(self.reference_mode),
            "mode_code": int(self.current_mode),
            "mode_name": MODE_NAMES.get(int(self.current_mode), "UNKNOWN"),
            "global_gain": int(self.gain),
            "channel_gains": [int(value) for value in self.channel_gains.tolist()],
            "channel_enabled": [bool(value) for value in self.channel_enabled.tolist()],
            "channel_bias": [bool(value) for value in self.channel_bias.tolist()],
            "channel_srb2": [bool(value) for value in self.channel_srb2.tolist()],
            "bias_register": self.bias_register_name(),
            "bias_mask": int(self.current_bias_mask()),
            "bias_mask_hex": f"0x{self.current_bias_mask():02X}",
        }

    def make_raw_path(self) -> str:
        """Preview the next automatic one-minute segment name."""
        now = datetime.now()
        return str(self._recording_folder() / f"{now:%m%d_%H%M}_xxxxxx_minute01.bin")

    # ---------------- processing state/helpers ----------------
    def reset_processing_state(self):
        self.filtered_ring.clear()
        if hasattr(self, "alpha_capture_kind"):
            self.alpha_capture_kind = None
            self.alpha_capture_values = []
        if hasattr(self, "open_btn"):
            self.open_btn.setEnabled(True)
            self.closed_btn.setEnabled(True)
        self.display_zi_band = np.zeros((CHANNELS, self.sos_display_band.shape[0], 2), dtype=float)
        self.display_zi_notch = np.zeros((CHANNELS, self.sos_notch.shape[0], 2), dtype=float)
        self.last_filter_input = np.zeros(CHANNELS, dtype=float)
        self.have_filter_input = np.zeros(CHANNELS, dtype=bool)
        self.filter_generation = int(getattr(self, "filter_generation", 0)) + 1
        worker = getattr(self, "filter_worker", None)
        if worker is not None:
            use_notch = (
                self.notch_check.isChecked()
                if hasattr(self, "notch_check") else True
            )
            worker.configure(
                self.filter_generation,
                self.sos_display_band,
                self.sos_notch,
                use_notch,
            )
        self.reset_psd_smoothing()

    def reset_psd_smoothing(self, *_args):
        self.psd_smooth_f: Optional[np.ndarray] = None
        self.psd_smooth_db: Optional[np.ndarray] = None
        self.psd_request_id += 1
        self.psd_last_signature = None

    @staticmethod
    def max_false_run(valid: np.ndarray) -> int:
        valid = np.asarray(valid, dtype=bool)
        if valid.size == 0 or valid.all():
            return 0
        bad = ~valid
        padded = np.r_[False, bad, False].astype(np.int8)
        edges = np.diff(padded)
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        return int(np.max(ends - starts)) if starts.size else 0

    def clean_with_valid(self, x: np.ndarray, valid: Optional[np.ndarray], max_gap: int = 2) -> Tuple[np.ndarray, bool, int]:
        x = np.asarray(x, dtype=float).copy()
        finite = np.isfinite(x)
        if valid is None or np.asarray(valid).size != x.size:
            good = finite
        else:
            good = finite & np.asarray(valid, dtype=bool)
        gap = self.max_false_run(good)
        if good.sum() < 2:
            return np.zeros_like(x), False, gap
        if not good.all():
            idx = np.arange(x.size)
            x[~good] = np.interp(idx[~good], idx[good], x[good])
        return x, bool(gap <= max_gap), gap

    def filter_offline_display(self, x: np.ndarray, valid: Optional[np.ndarray]) -> np.ndarray:
        x, _gap_ok, _ = self.clean_with_valid(x, valid, max_gap=2)
        x = signal.detrend(x, type="constant")
        if x.size < 64:
            return x
        try:
            y = signal.sosfiltfilt(self.sos_display_band, x)
            if not hasattr(self, "notch_check") or self.notch_check.isChecked():
                y = signal.sosfiltfilt(self.sos_notch, y)
        except ValueError:
            y = signal.sosfilt(self.sos_display_band, x)
            if not hasattr(self, "notch_check") or self.notch_check.isChecked():
                y = signal.sosfilt(self.sos_notch, y)
        return y

    def filter_offline_view(self, start: int, end: int) -> np.ndarray:
        """Filter an offline view with context, then crop it to the visible range.

        Filtering only the visible samples makes ``sosfiltfilt`` treat both
        screen edges as signal boundaries.  Its startup/ending transient then
        looks like a real amplitude wobble.  Extra samples (or reflected data
        at the actual file boundaries) keep that transient outside the view.
        """
        view_len = max(0, end - start)
        if self.offline_uv is None or view_len == 0:
            return np.empty((CHANNELS, 0), dtype=float)

        hp = max(0.1, float(self.hp_spin.value()))
        context = max(3 * FS, int(np.ceil(5.0 * FS / hp)))
        source_len = self.offline_uv.shape[1]
        context_start = max(0, start - context)
        context_end = min(source_len, end + context)
        values = self.offline_uv[:, context_start:context_end].astype(float, copy=True)
        valid = self.offline_valid[context_start:context_end].copy()

        left_missing = max(0, context - (start - context_start))
        right_missing = max(0, context - (context_end - end))
        if left_missing or right_missing:
            # Reflecting the real endpoint gives the zero-phase filter enough
            # settling data without inventing a step or changing saved data.
            pad_mode = "reflect" if values.shape[1] > 1 else "edge"
            values = np.pad(values, ((0, 0), (left_missing, right_missing)), mode=pad_mode)
            valid = np.pad(valid, (left_missing, right_missing), mode="edge")

        crop_start = left_missing + (start - context_start)
        crop_end = crop_start + view_len
        filtered = np.vstack([
            self.filter_offline_display(values[ch], valid)
            for ch in range(CHANNELS)
        ])
        result = filtered[:, crop_start:crop_end]
        view_valid = self.offline_valid[start:end]
        if view_valid.size == result.shape[1] and not np.all(view_valid):
            result[:, ~view_valid] = np.nan
        return result

    def filter_for_psd(
        self,
        x: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> np.ndarray:
        """Apply the toolbar's current band-pass/notch settings for PSD."""
        x = signal.detrend(np.asarray(x, dtype=float), type="linear")
        if x.size < 64:
            return x
        band = self.sos_display_band if sos_band is None else np.asarray(sos_band)
        notch_enabled = (
            self.notch_check.isChecked()
            if use_notch is None and hasattr(self, "notch_check")
            else bool(use_notch)
        )
        try:
            y = signal.sosfiltfilt(band, x)
            if notch_enabled:
                y = signal.sosfiltfilt(self.sos_notch, y)
        except ValueError:
            y = signal.sosfilt(band, x)
            if notch_enabled:
                y = signal.sosfilt(self.sos_notch, y)
        return y

    def append_live_filtered(
        self,
        frames: List[Frame],
        values: Optional[np.ndarray] = None,
        valid: Optional[np.ndarray] = None,
        sequence: Optional[np.ndarray] = None,
        modes: Optional[np.ndarray] = None,
    ):
        if not frames:
            return
        if values is None:
            values = np.stack([fr.uv for fr in frames], axis=1).astype(np.float32)
        if valid is None:
            valid = np.array([fr.valid for fr in frames], dtype=bool)
        if sequence is None:
            sequence = np.array([fr.sequence for fr in frames], dtype=np.uint32)
        if modes is None:
            modes = np.array([fr.mode for fr in frames], dtype=np.uint8)
        source = np.asarray(values, dtype=float)
        channel_good_matrix = np.asarray(valid, dtype=bool)[None, :] & np.isfinite(source)
        filled = source.copy()
        # Missing/saturated samples are forward-filled only for evolving the
        # causal filter state. The corresponding channel output is restored to
        # NaN below, so no EEG value is invented.
        for ch in range(CHANNELS):
            channel_good = channel_good_matrix[ch]
            if not self.have_filter_input[ch]:
                first_candidates = np.flatnonzero(channel_good)
                if first_candidates.size:
                    first_idx = int(first_candidates[0])
                    first_value = float(filled[ch, first_idx])
                    filled[ch, :first_idx] = first_value
                    self.display_zi_band[ch] = signal.sosfilt_zi(self.sos_display_band) * first_value
                    self.display_zi_notch[ch].fill(0.0)
                    self.last_filter_input[ch] = first_value
                    self.have_filter_input[ch] = True
            if channel_good.all():
                if filled.shape[1]:
                    self.last_filter_input[ch] = float(filled[ch, -1])
                    self.have_filter_input[ch] = True
            else:
                n_samples = int(filled.shape[1])
                if n_samples:
                    original = filled[ch].copy()
                    seed = float(self.last_filter_input[ch]) if self.have_filter_input[ch] else 0.0
                    last_good_index = np.where(
                        channel_good, np.arange(n_samples, dtype=np.int64), -1
                    )
                    np.maximum.accumulate(last_good_index, out=last_good_index)
                    has_previous = last_good_index >= 0
                    filled[ch, ~has_previous] = seed
                    if np.any(has_previous):
                        filled[ch, has_previous] = original[last_good_index[has_previous]]
                    good_indices = np.flatnonzero(channel_good)
                    if good_indices.size:
                        self.last_filter_input[ch] = float(original[int(good_indices[-1])])
                        self.have_filter_input[ch] = True

        # SciPy accepts all eight channels in one call when the sample axis is 1.
        # State is transposed from (channel, section, 2) to the required
        # (section, channel, 2), cutting the hot path from 16 sosfilt calls per
        # BLE batch to only two calls.
        band_zi = np.transpose(self.display_zi_band, (1, 0, 2))
        filtered, band_zf = signal.sosfilt(
            self.sos_display_band, filled, axis=1, zi=band_zi
        )
        self.display_zi_band = np.transpose(band_zf, (1, 0, 2))
        if not hasattr(self, "notch_check") or self.notch_check.isChecked():
            notch_zi = np.transpose(self.display_zi_notch, (1, 0, 2))
            filtered, notch_zf = signal.sosfilt(
                self.sos_notch, filtered, axis=1, zi=notch_zi
            )
            self.display_zi_notch = np.transpose(notch_zf, (1, 0, 2))

        # One non-finite value must not poison an IIR state forever and make all
        # later waveforms disappear.  Reset only the affected channel state and
        # pass a finite fallback through this display batch; validity masks still
        # preserve real packet gaps.
        bad_channels = np.flatnonzero(
            ~np.all(np.isfinite(filtered), axis=1)
            | ~np.all(np.isfinite(self.display_zi_band), axis=(1, 2))
            | ~np.all(np.isfinite(self.display_zi_notch), axis=(1, 2))
        )
        for ch in bad_channels:
            seed = float(self.last_filter_input[ch]) if np.isfinite(self.last_filter_input[ch]) else 0.0
            self.display_zi_band[ch] = signal.sosfilt_zi(self.sos_display_band) * seed
            self.display_zi_notch[ch].fill(0.0)
            filtered[ch] = np.nan_to_num(filtered[ch], nan=seed, posinf=seed, neginf=seed)
        filtered[~channel_good_matrix] = np.nan
        self.filtered_ring.append_batch(
            np.asarray(filtered, dtype=np.float32), valid, sequence, modes
        )

    def smooth_psd_db(self, f: np.ndarray, p: np.ndarray) -> np.ndarray:
        now_db = 10.0 * np.log10(np.asarray(p, dtype=float) + np.finfo(float).eps)
        if (
            self.psd_smooth_f is None
            or self.psd_smooth_db is None
            or self.psd_smooth_f.shape != f.shape
            or not np.allclose(self.psd_smooth_f, f)
        ):
            self.psd_smooth_f = np.asarray(f, dtype=float).copy()
            self.psd_smooth_db = now_db.copy()
        else:
            beta = float(self.psd_smooth_beta)
            self.psd_smooth_db = beta * self.psd_smooth_db + (1.0 - beta) * now_db
        return self.psd_smooth_db.copy()

    def assess_eeg_window(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
    ) -> Tuple[bool, str, np.ndarray]:
        valid = np.asarray(valid, dtype=bool)
        valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        cleaned, gap_ok, max_gap = self.clean_with_valid(x, valid, max_gap=2)
        if valid_ratio < 0.99:
            return False, f"有效样本仅 {100*valid_ratio:.2f}%", cleaned
        if not gap_ok:
            return False, f"连续缺失 {max_gap} 点（只允许 <=2）", cleaned
        if seq.size > 1:
            delta = (seq[1:].astype(np.uint64) - seq[:-1].astype(np.uint64)) & np.uint64(0xFFFFFFFF)
            if np.any(delta != 1):
                return False, "片段内存在序号跳变", cleaned
        if mode.size and np.any(mode != mode[-1]):
            return False, "片段内切换过采集模式", cleaned
        current_mode = int(mode[-1]) if mode.size else self.current_mode
        full_scale_uv = VREF / max(self.gain, 1) * 1e6
        if np.max(np.abs(cleaned)) > 0.95 * full_scale_uv:
            return False, "接近 ADC 满量程", cleaned
        if current_mode in (0, 1, 2):
            p2p = float(np.ptp(cleaned))
            if p2p > 250.0:
                return False, f"峰峰值 {p2p:.1f} uV，疑似眨眼/运动", cleaned
            if float(np.std(cleaned)) < 0.01:
                return False, "近似平线/通道未工作", cleaned
        elif current_mode in (3, 4):
            return False, "SHORTED/TEST 模式不计算 Alpha", cleaned
        return True, "片段质量合格", cleaned

    def compute_live_psd_fast(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> Tuple[bool, str, dict]:
        """Cheap, saturation-tolerant PSD for the live path.

        Exactly one raw Welch and one filtered Welch are evaluated. The older
        quality-window loop could execute many additional Welch transforms per
        refresh; that is useful offline but unnecessary for a live monitor.
        Saturated samples remain finite and are intentionally analysed instead
        of pausing PSD or generating extra jobs.
        """
        x = np.asarray(x, dtype=float)
        valid = np.asarray(valid, dtype=bool)
        cleaned, _gap_ok, max_gap = self.clean_with_valid(x, valid, max_gap=2)
        metrics = {
            "cleaned": cleaned,
            "raw_f": np.array([]),
            "raw_p": np.array([]),
            "display_f": np.array([]),
            "display_p": np.array([]),
            "alpha_f": np.array([]),
            "alpha_p": np.array([]),
            "alpha_power": np.nan,
            "alpha_peak": np.nan,
            "alpha_rel": np.nan,
            "filtered_rms": np.nan,
            "good_segments": 0,
            "total_segments": 1,
        }
        if cleaned.size < FS * 4:
            return False, "不足 4 秒", metrics
        nperseg = min(cleaned.size, FS * 4)
        nfft = 1024
        noverlap = min(nperseg // 2, nperseg - 1)
        raw_f, raw_p = signal.welch(
            cleaned, fs=FS, window="hann", nperseg=nperseg,
            noverlap=noverlap, nfft=nfft, detrend=False,
        )
        display_signal = self.filter_for_psd(
            cleaned, sos_band=sos_band, use_notch=use_notch
        )
        display_f, display_p = signal.welch(
            display_signal, fs=FS, window="hann", nperseg=nperseg,
            noverlap=noverlap, nfft=nfft,
        )
        metrics["raw_f"] = raw_f
        metrics["raw_p"] = raw_p
        metrics["display_f"] = display_f
        metrics["display_p"] = display_p
        metrics["alpha_f"] = display_f
        metrics["alpha_p"] = display_p
        metrics["filtered_rms"] = float(np.sqrt(np.mean(display_signal**2)))
        alpha = (display_f >= 8) & (display_f <= 13)
        broad = (display_f >= 4) & (display_f <= 30)
        if np.any(alpha) and np.any(broad):
            alpha_power = float(np.trapezoid(display_p[alpha], display_f[alpha]))
            broad_power = float(np.trapezoid(display_p[broad], display_f[broad]))
            af = display_f[alpha]
            metrics["alpha_power"] = alpha_power
            metrics["alpha_peak"] = float(af[int(np.argmax(display_p[alpha]))])
            metrics["alpha_rel"] = alpha_power / max(broad_power, np.finfo(float).eps)
        sat = self.saturation_mask_uv(cleaned[None, :])[0]
        sat_ratio = float(np.mean(sat)) if sat.size else 0.0
        valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        metrics["good_segments"] = 1
        if sat_ratio > 0.0:
            reason = f"PSD持续计算；饱和样本 {sat_ratio*100:.1f}%"
            return False, reason, metrics
        if valid_ratio < 0.99 or max_gap > 2:
            return False, f"PSD持续计算；有效样本 {valid_ratio*100:.1f}%", metrics
        return True, "实时 PSD 正常", metrics

    def compute_alpha_from_window(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> Tuple[bool, str, dict]:
        x = np.asarray(x, dtype=float)
        valid = np.asarray(valid, dtype=bool)
        self.latest_valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        cleaned_all, _gap_ok, _max_gap = self.clean_with_valid(x, valid, max_gap=2)
        metrics = {
            "cleaned": cleaned_all,
            "raw_f": np.array([]),
            "raw_p": np.array([]),
            "display_f": np.array([]),
            "display_p": np.array([]),
            "alpha_f": np.array([]),
            "alpha_p": np.array([]),
            "alpha_power": np.nan,
            "alpha_peak": np.nan,
            "alpha_rel": np.nan,
            "filtered_rms": np.nan,
            "good_segments": 0,
            "total_segments": 0,
        }
        segment_len = FS * 4
        step = FS  # 75% overlap
        if cleaned_all.size < segment_len:
            return False, "不足 4 秒", metrics

        # Raw diagnostic PSD uses the input samples without band-pass, notch,
        # or detrending. Welch still applies its analysis window and averaging.
        nfft = max(2048, 2 ** int(np.ceil(np.log2(segment_len))))
        raw_f, raw_p = signal.welch(
            cleaned_all,
            fs=FS,
            window="hann",
            nperseg=segment_len,
            noverlap=3 * segment_len // 4,
            nfft=nfft,
            detrend=False,
        )
        metrics["raw_f"] = raw_f
        metrics["raw_p"] = raw_p

        # Prepare the default PSD with the toolbar's current filter settings.
        # Alpha quality
        # windows may all be rejected (for example during movement); that
        # should change the quality verdict, not leave the spectrum blank.
        display_signal = self.filter_for_psd(
            cleaned_all, sos_band=sos_band, use_notch=use_notch
        )
        display_f, display_p = signal.welch(
            display_signal,
            fs=FS,
            window="hann",
            nperseg=segment_len,
            noverlap=3 * segment_len // 4,
            nfft=nfft,
        )
        metrics["display_f"] = display_f
        metrics["display_p"] = display_p
        metrics["filtered_rms"] = float(np.sqrt(np.mean(display_signal**2)))

        # Always expose Alpha peak/rate from the current Alpha analysis chain.
        # Window-quality screening is still retained for the optional 20-second
        # capture workflow, but it no longer blanks the PSD's basic metrics.
        display_alpha = (display_f >= 8) & (display_f <= 13)
        display_broad = (display_f >= 4) & (display_f <= 30)
        if np.any(display_alpha) and np.any(display_broad):
            display_alpha_power = float(
                np.trapezoid(display_p[display_alpha], display_f[display_alpha])
            )
            display_broad_power = float(
                np.trapezoid(display_p[display_broad], display_f[display_broad])
            )
            display_af = display_f[display_alpha]
            metrics["alpha_power"] = display_alpha_power
            metrics["alpha_peak"] = float(
                display_af[int(np.argmax(display_p[display_alpha]))]
            )
            metrics["alpha_rel"] = display_alpha_power / max(
                display_broad_power, np.finfo(float).eps
            )

        segment_psds: List[np.ndarray] = []
        segment_rms: List[float] = []
        alpha_f: Optional[np.ndarray] = None
        reject_reasons: List[str] = []
        starts = list(range(0, cleaned_all.size - segment_len + 1, step))
        metrics["total_segments"] = len(starts)
        for start in starts:
            end = start + segment_len
            good, reason, segment_x = self.assess_eeg_window(
                x[start:end], valid[start:end], seq[start:end], mode[start:end]
            )
            if not good:
                reject_reasons.append(reason)
                continue
            alpha_signal = self.filter_for_psd(
                segment_x, sos_band=sos_band, use_notch=use_notch
            )
            f_seg, p_seg = signal.welch(
                alpha_signal,
                fs=FS,
                window="hann",
                nperseg=segment_len,
                noverlap=3 * segment_len // 4,
                nfft=nfft,
            )
            alpha_f = f_seg
            segment_psds.append(p_seg)
            segment_rms.append(float(np.sqrt(np.mean(alpha_signal**2))))

        metrics["good_segments"] = len(segment_psds)
        required = 1 if len(starts) == 1 else 3
        if len(segment_psds) < required or alpha_f is None:
            common = max(set(reject_reasons), key=reject_reasons.count) if reject_reasons else "无合格片段"
            return False, f"仅 {len(segment_psds)}/{len(starts)} 个合格4秒片段：{common}", metrics

        # Median across accepted segments is more robust than averaging a blink/EMG-contaminated segment.
        alpha_p = np.median(np.stack(segment_psds, axis=0), axis=0)
        metrics["alpha_f"] = alpha_f
        metrics["alpha_p"] = alpha_p
        metrics["filtered_rms"] = float(np.median(segment_rms))

        alpha = (alpha_f >= 8) & (alpha_f <= 13)
        broad = (alpha_f >= 4) & (alpha_f <= 30)
        if np.any(alpha) and np.any(broad):
            alpha_power = float(np.trapezoid(alpha_p[alpha], alpha_f[alpha]))
            broad_power = float(np.trapezoid(alpha_p[broad], alpha_f[broad]))
            af = alpha_f[alpha]
            metrics["alpha_power"] = alpha_power
            metrics["alpha_peak"] = float(af[int(np.argmax(alpha_p[alpha]))])
            metrics["alpha_rel"] = alpha_power / max(broad_power, np.finfo(float).eps)
        return True, f"{len(segment_psds)}/{len(starts)} 个4秒片段通过", metrics

    # ---------------- transport/actions ----------------
    def open_impedance_dialog(self):
        if self.impedance_dialog is not None:
            self.impedance_dialog.show()
            self.impedance_dialog.raise_()
            self.impedance_dialog.activateWindow()
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("电极阻抗检测")
        dialog.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel(
            "ADS1299 交流导联脱落检测：6 nA @ 31.25 Hz。"
            "SRB2 时激励 INxN，SRB1 时激励 INxP；检测期间不写入 EEG BIN。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        calibration = QtWidgets.QHBoxLayout()
        calibration.addWidget(QtWidgets.QLabel("板载输入串联电阻补偿"))
        self.impedance_series_spin = QtWidgets.QDoubleSpinBox()
        self.impedance_series_spin.setRange(0.0, 100.0)
        self.impedance_series_spin.setDecimals(2)
        self.impedance_series_spin.setSingleStep(0.01)
        self.impedance_series_spin.setValue(self.impedance_series_default_kohm())
        self.impedance_series_spin.setSuffix(" kΩ")
        self.sync_impedance_series_compensation()
        calibration.addWidget(self.impedance_series_spin)
        calibration.addStretch(1)
        layout.addLayout(calibration)

        table = QtWidgets.QTableWidget(CHANNELS, 3)
        table.setHorizontalHeaderLabels(["通道", "阻抗", "接触质量"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.impedance_checks = []
        self.impedance_value_labels = []
        self.impedance_quality_labels = []
        for ch in range(CHANNELS):
            check = QtWidgets.QCheckBox(f"CH{ch + 1}")
            check.setChecked(bool(self.channel_enabled[ch]))
            check.setEnabled(bool(self.channel_enabled[ch]) and not self.impedance_active)
            value = QtWidgets.QLabel("等待检测")
            value.setAlignment(QtCore.Qt.AlignCenter)
            quality = QtWidgets.QLabel("—")
            quality.setAlignment(QtCore.Qt.AlignCenter)
            table.setCellWidget(ch, 0, check)
            table.setCellWidget(ch, 1, value)
            table.setCellWidget(ch, 2, quality)
            self.impedance_checks.append(check)
            self.impedance_value_labels.append(value)
            self.impedance_quality_labels.append(quality)
        layout.addWidget(table)

        legend = QtWidgets.QLabel("判定：良好 < 10 kΩ；可用 10–50 kΩ；接触不良 > 50 kΩ")
        legend.setStyleSheet("color:#555;")
        layout.addWidget(legend)
        buttons = QtWidgets.QHBoxLayout()
        start = QtWidgets.QPushButton("开始检测")
        stop = QtWidgets.QPushButton("停止并关闭激励")
        close = QtWidgets.QPushButton("关闭")
        start.clicked.connect(self.start_impedance_detection)
        stop.clicked.connect(self.stop_impedance_detection)
        close.clicked.connect(dialog.close)
        buttons.addWidget(start)
        buttons.addWidget(stop)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.impedance_dialog = dialog
        dialog.finished.connect(self._impedance_dialog_finished)
        dialog.show()

    def _impedance_dialog_finished(self, _result):
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        self.impedance_dialog = None
        self.impedance_checks = []
        self.impedance_value_labels = []
        self.impedance_quality_labels = []
        self.impedance_series_spin = None

    def selected_impedance_mask(self) -> int:
        mask = 0
        for ch, check in enumerate(self.impedance_checks):
            if check.isChecked() and self.channel_enabled[ch]:
                mask |= 1 << ch
        return mask

    def start_impedance_detection(self):
        if not self.require_transport():
            return
        if self.current_mode not in (0, 1, 2):
            QtWidgets.QMessageBox.warning(
                self, "阻抗检测", "请先切换到 EEG 模式；短路或内部测试模式不能测量电极阻抗。"
            )
            return
        mask = self.selected_impedance_mask()
        if mask == 0:
            QtWidgets.QMessageBox.warning(self, "阻抗检测", "请至少勾选一个已启用通道。")
            return
        try:
            # A measurement carrier must never be mixed into a normal EEG
            # recording. Finalize an existing file before enabling LOFF.
            if self.streaming:
                self.transport_write(b"s")
                self.streaming = False
                self.close_raw_file()
                time.sleep(0.08)
            self.transport_reset_input_buffer()
            self.parser.reset()
            if self.active_transport == "ble":
                self.impedance_active = True
                self.impedance_mask = mask
                ack = self._ble_write_bulk_config(REFERENCE_SRB1)
                snapshot = self.ble_worker.config_snapshot
                expected_p = mask
                expected_n = 0
                confirmed = (
                    snapshot is not None
                    and snapshot.verified
                    and snapshot.lead_off_p == expected_p
                    and snapshot.lead_off_n == expected_n
                )
            else:
                self.transport_write(bytes((0xA9, mask & 0xFF)))
                ack = self.read_config_ack(0xA9, expected_argument=mask & 0xFF)
                expected_p = mask
                expected_n = 0
                confirmed = (
                    ack is not None
                    and ack["verified"]
                    and ack["loff_p"] == expected_p
                    and ack["loff_n"] == expected_n
                    and ack["loff_config"] == 0x02
                )
            if (
                not confirmed
            ):
                raise RuntimeError(
                    "固件未确认 LOFF 寄存器。请烧录本版本配套固件后重试。"
                )
            self.ring.clear()
            self.filtered_ring.clear()
            self.reset_processing_state()
            self.last_seq = None
            self.first_seq = None
            self.first_clock = None
            self.transport_reset_input_buffer()
            self.transport_write(b"b")
            self.streaming = True
            self.impedance_active = True
            self.impedance_mask = mask
            for check in self.impedance_checks:
                check.setEnabled(False)
            self.impedance_timer.start()
            side = "INxN / LOFF_SENSN" if self.reference_is_srb2() else "INxP / LOFF_SENSP"
            self.set_status(f"阻抗检测中：mask=0x{mask:02X}，激励端 {side}。")
        except Exception as exc:
            try:
                if self.transport_connected():
                    self.transport_write(b"s")
                    time.sleep(0.05)
                    self.transport_reset_input_buffer()
                    if self.active_transport == "ble":
                        self.impedance_active = False
                        self.impedance_mask = 0
                        self._ble_write_bulk_config(REFERENCE_SRB1)
                    else:
                        self.transport_write(bytes((0xA9, 0x00)))
            except Exception:
                pass
            self.impedance_active = False
            self.impedance_mask = 0
            QtWidgets.QMessageBox.critical(self, "阻抗检测启动失败", str(exc))

    def stop_impedance_detection(self, _checked=False, silent=False):
        if not self.impedance_active:
            return
        error = None
        try:
            if self.transport_connected():
                self.transport_write(b"s")
                self.streaming = False
                time.sleep(0.08)
                self.transport_reset_input_buffer()
                if self.active_transport == "ble":
                    self.impedance_active = False
                    self.impedance_mask = 0
                    self._ble_write_bulk_config(REFERENCE_SRB1)
                    snapshot = self.ble_worker.config_snapshot
                    if snapshot is None or snapshot.lead_off_p != 0 or snapshot.lead_off_n != 0:
                        error = "ADS1299 未确认 LOFF 已关闭"
                else:
                    self.transport_write(bytes((0xA9, 0x00)))
                    ack = self.read_config_ack(0xA9, expected_argument=0)
                    if (
                        ack is None
                        or not ack["verified"]
                        or ack["loff_p"] != 0
                        or ack["loff_n"] != 0
                        or ack["loff_config"] != 0
                    ):
                        error = "ADS1299 未确认 LOFF 已关闭"
        except Exception as exc:
            error = str(exc)
        finally:
            self.impedance_active = False
            self.impedance_mask = 0
            self.streaming = False
            self.impedance_timer.stop()
            for ch, check in enumerate(self.impedance_checks):
                check.setEnabled(bool(self.channel_enabled[ch]))
        if error:
            if not silent:
                QtWidgets.QMessageBox.warning(self, "关闭阻抗检测", error)
            self.set_status(f"阻抗检测已停止，但关闭确认失败：{error}")
        else:
            self.set_status("阻抗检测已停止，LOFF 激励已由 ADS1299 读回确认关闭。")

    def update_impedance_results(self):
        if not self.impedance_active or not self.impedance_value_labels:
            return
        values, valid, _seq, _mode = self.ring.latest(FS * 4)
        if values.shape[1] < FS:
            for ch in range(CHANNELS):
                if self.impedance_mask & (1 << ch):
                    self.impedance_value_labels[ch].setText("稳定中…")
            return
        t = np.arange(values.shape[1], dtype=float) / FS
        carrier = np.column_stack((
            np.sin(2.0 * np.pi * LEAD_OFF_FREQUENCY_HZ * t),
            np.cos(2.0 * np.pi * LEAD_OFF_FREQUENCY_HZ * t),
            np.ones_like(t),
        ))
        for ch in range(CHANNELS):
            value_label = self.impedance_value_labels[ch]
            quality_label = self.impedance_quality_labels[ch]
            if not (self.impedance_mask & (1 << ch)):
                value_label.setText("未选择")
                quality_label.setText("—")
                quality_label.setStyleSheet("")
                continue
            good = valid & np.isfinite(values[ch])
            if np.count_nonzero(good) < FS:
                value_label.setText("数据不足")
                quality_label.setText("—")
                quality_label.setStyleSheet("")
                continue
            coeff, *_ = np.linalg.lstsq(carrier[good], values[ch, good], rcond=None)
            carrier_peak_uv = float(np.hypot(coeff[0], coeff[1]))
            series_kohm = (
                float(self.impedance_series_spin.value())
                if self.impedance_series_spin is not None
                else self.impedance_series_default_kohm()
            )
            impedance_kohm = max(
                0.0, carrier_peak_uv / LEAD_OFF_CURRENT_NA - series_kohm
            )
            value_label.setText(
                ">999 kΩ" if impedance_kohm > 999.0 else f"{impedance_kohm:.1f} kΩ"
            )
            if impedance_kohm < 10.0:
                text, color = "良好", "#258b3b"
            elif impedance_kohm <= 50.0:
                text, color = "可用", "#d97800"
            else:
                text, color = "接触不良", "#c62828"
            quality_label.setText(text)
            quality_label.setStyleSheet(f"color:{color};font-weight:700;")

    def reset_live_session_metrics(self):
        """Reset every counter that must describe only the next recording."""
        self.packet_count = 0
        self.live_sample_count = 0
        self.status_bad = 0
        self.drdy_bad = 0
        self.seq_lost = 0
        self.seq_gap_events = 0
        self.seq_device_lost = 0
        self.seq_host_lost = 0
        self.timeline_gap_samples = 0
        self.timeline_gap_events = 0
        self.timeline_large_discontinuities = 0
        self.live_timeline_sample_count = 0
        self.backlog_events = 0
        self.queue_drop_hints = 0
        self.saturation_samples = 0
        self.saturation_channel_samples = np.zeros(CHANNELS, dtype=np.int64)
        self.last_visible_saturated_channels: Tuple[int, ...] = tuple()
        self.last_seq = None
        self.last_queue_drop_low = 0
        self.first_seq = None
        self.first_clock = None
        self.fs_est = np.nan
        self.last_read_us = 0
        self.max_read_us = 0
        self.last_pending = 0
        self.last_queue_depth = 0
        self.last_serial_waiting_bytes = 0
        self.live_lag_s = 0.0
        self.transport_peak_pending_bytes = 0
        self.serial_catchup_skips = 0
        self.transport_last_turn_ms = 0.0
        self.transport_max_turn_ms = 0.0
        self.ble_coalesced_batches = 0
        self.ble_catchup_plot_skips = 0
        self.ble_psd_skips = 0
        self.ble_batch_started_monotonic = None
        self.raw_write_errors = 0
        self.plot_errors = 0
        self._last_live_plot_packet = -1
        self.session_started_monotonic = time.monotonic()
        # Clear the locally displayed firmware counters immediately. The 'r'
        # command below resets the matching counters on the C3 itself.
        for key in (
            "queue_drop", "notify_error", "command_drop", "mtu_blocked", "blocks_sent",
            "reliable_stored", "reliable_outstanding", "reliable_ack_count",
            "reliable_nack_count", "reliable_retransmit", "reliable_recovered",
            "reliable_overflow", "reliable_unknown_nack", "reliable_protocol_error",
        ):
            if key in self.ble_status:
                self.ble_status[key] = 0
        self.ble_status_delta = {}

    def start_stream(self):
        if not self.require_transport():
            return
        if self.active_transport == "ble" and int(self.ble_status.get("status_protocol", 0)) not in (0x03, 0x04):
            QtWidgets.QMessageBox.warning(
                self,
                "BLE 固件不匹配",
                "V18 BLE 模式请使用本压缩包内配套 V18 固件。旧 reliable 固件可连接，但不包含采集/TX 隔离和过期 NACK 抑制。",
            )
            return
        if (
            self.active_transport == "ble"
            and self.ble_status
            and int(self.ble_status.get("mtu", 23)) < BLE_MIN_STREAM_MTU
        ):
            QtWidgets.QMessageBox.warning(
                self,
                "BLE MTU 太小",
                f"当前 MTU={self.ble_status.get('mtu', 23)}，配套固件要求至少 100 才发送 EEG。"
                "请断开后重新连接，关闭其他蓝牙软件，或更新电脑蓝牙驱动。",
            )
            return
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        try:
            self.offline_uv = None
            self.offline_slider.setEnabled(False)
            self.offline_label.setText("实时")
            self.parser.reset()
            self.ring.clear()
            self.reset_processing_state()
            self.reset_live_session_metrics()
            self.reset_display_jitter_buffer()
            if self.active_transport == "serial":
                self.display_buffer_state = "direct"
                self.display_target_delay_samples = 0
                self.display_startup_samples = 0
            if self.ble_worker is not None:
                self.ble_worker.reset_timing_metrics()
                self.ble_worker.set_streaming_hint(True)
            self.raw_writer.start_session(
                str(self._recording_folder()),
                self._recording_configuration_snapshot(),
            )
            snap = self.raw_writer.snapshot()
            self.raw_path = str(snap.get("current_path", ""))
            self.recording_session_id = str(snap.get("session_id", ""))
            self.recording_manifest_path = str(snap.get("manifest_path", ""))
            self.recording_segment_index = int(snap.get("segment_index", 1))
            if self.stream_server is not None:
                self.stream_server.begin_recording(
                    self.recording_session_id,
                    started_at=time.time(),
                )
            self.raw_recording_enabled = True
            self.raw_write_errors = 0
            self.raw_file = True
            self.raw_bytes = 0
            self.transport_reset_input_buffer()
            if self.active_transport == "ble":
                self.transport_write(b"r")
                time.sleep(0.05)
                self.transport_reset_input_buffer(clear_status=False, reset_reliable=True)
            self.transport_write(b"b")
            self.streaming = True
            self.set_status(
                f"实时采集中：ID {self.recording_session_id}，每 60 秒自动分包，"
                f"当前 {Path(self.raw_path).name}"
            )
        except Exception as exc:
            if self.stream_server is not None:
                self.stream_server.end_recording()
            self.close_raw_file()
            QtWidgets.QMessageBox.critical(self, "开始失败", str(exc))

    def stop_stream(self, offer_export: bool = False):
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
            return
        was_streaming = bool(self.streaming)
        first_path = self.raw_writer.first_path or self.raw_path
        if self.transport_connected():
            try:
                self.transport_write(b"s")
            except Exception:
                pass
        self.streaming = False
        if self.ble_worker is not None:
            self.ble_worker.set_streaming_hint(False)
        self.close_raw_file()
        if self.stream_server is not None:
            self.stream_server.end_recording()
        snap = self.raw_writer.snapshot()
        segment_count = int(snap.get("segment_count", 0))
        if was_streaming and self.recording_session_id:
            manifest_name = (
                Path(self.recording_manifest_path).name
                if self.recording_manifest_path else "---"
            )
            self.set_status(
                f"采集已停止：ID {self.recording_session_id}，"
                f"共 {segment_count} 个一分钟 BIN；清单 {manifest_name}"
            )
        if (
            offer_export
            and was_streaming
            and first_path
            and Path(first_path).exists()
            and Path(first_path).stat().st_size
        ):
            if QtWidgets.QMessageBox.question(
                self,
                "采集完成",
                "分包 BIN 已保存。是否转换第一个一分钟文件为 BDF 或 MNE FIF？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            ) == QtWidgets.QMessageBox.Yes:
                self.export_biosignal_formats(first_path)

    def enqueue_raw_bytes(self, data: bytes):
        """Queue raw bytes; rotation and metadata remain off the Qt thread."""
        if not self.raw_recording_enabled or not data:
            return
        if self.raw_writer.submit(data):
            self.raw_bytes += len(data)
            snap = self.raw_writer.snapshot()
            current_path = str(snap.get("current_path", ""))
            if current_path:
                self.raw_path = current_path
            self.recording_segment_index = int(snap.get("segment_index", self.recording_segment_index))
            return
        self.raw_recording_enabled = False
        self.raw_write_errors += 1
        self.raw_file = None
        error = self.raw_writer.error or "原始 BIN 写盘线程不可用"
        self.set_status(f"{error}；实时波形和传输继续运行。")

    def close_raw_file(self):
        if self.raw_recording_enabled or self.raw_file is not None:
            self.raw_recording_enabled = False
            try:
                self.raw_writer.stop(timeout=15.0)
            except Exception:
                self.raw_write_errors += 1
        snap = self.raw_writer.snapshot()
        current_path = str(snap.get("current_path", ""))
        if current_path:
            self.raw_path = current_path
        self.recording_manifest_path = str(snap.get("manifest_path", self.recording_manifest_path))
        self.recording_segment_index = int(snap.get("segment_index", self.recording_segment_index))
        self.raw_file = None

    def _mode_index_from_code(self, mode_code: int) -> int:
        for index, (_name, _cmd, expected) in enumerate(MODE_ITEMS):
            if expected == int(mode_code):
                return index
        return 0

    def _sync_internal_short_button(self, active: bool):
        if not hasattr(self, "internal_short_btn"):
            return
        active = bool(active)
        expected_text = "退出短接" if active else "内部短接"
        if (
            self.internal_short_btn.isChecked() == active
            and self.internal_short_btn.text() == expected_text
        ):
            return
        self._syncing_internal_short_button = True
        try:
            self.internal_short_btn.blockSignals(True)
            self.internal_short_btn.setChecked(active)
            self.internal_short_btn.setText(expected_text)
            self.internal_short_btn.setStyleSheet(
                "QPushButton{background:#ff5a01;color:white;border:1px solid #c94700;"
                "font-weight:700;}"
                if active
                else ""
            )
        finally:
            self.internal_short_btn.blockSignals(False)
            self._syncing_internal_short_button = False

    def _switch_frontend_mode(self, idx: int) -> bool:
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        if not self.require_transport():
            return False
        idx = int(np.clip(idx, 0, len(MODE_ITEMS) - 1))
        name, cmd, expected = MODE_ITEMS[idx]
        was_streaming = self.streaming
        try:
            self.transport_write(b"s")
            self.streaming = False
            time.sleep(0.08)
            self.transport_reset_input_buffer()
            self.parser.reset()
            self.transport_write(cmd)
            if cmd in (b"q", b"t"):
                # Diagnostic modes should show the unfiltered ADC behavior.
                self.filter_check.setChecked(False)
                self.psd_raw_check.setChecked(True)
            self.ring.clear()
            self.filtered_ring.clear()
            self.reset_processing_state()
            self.last_seq = None
            self.first_seq = None
            self.first_clock = None
            self.fs_est = np.nan
            self.current_mode = expected
            time.sleep(0.35)
            self.transport_reset_input_buffer()
            if was_streaming:
                self.transport_write(b"b")
                self.streaming = True
            self.mode_combo.setCurrentIndex(idx)
            self._sync_internal_short_button(expected == 3)
            self.set_status(f"模式已切换：{name}")
            return True
        except Exception as exc:
            self.streaming = False
            self._sync_internal_short_button(self.current_mode == 3)
            QtWidgets.QMessageBox.critical(self, "模式切换失败", str(exc))
            return False

    def toggle_internal_short(self, checked: bool):
        """Enter ADS1299 internal input short with one click; click again to restore EEG."""
        if self._syncing_internal_short_button:
            return
        if checked:
            if self.current_mode in (0, 1, 2):
                self.mode_before_internal_short = int(self.current_mode)
            target_mode = 3
        else:
            target_mode = (
                int(self.mode_before_internal_short)
                if self.mode_before_internal_short in (0, 1, 2)
                else 0
            )
        ok = self._switch_frontend_mode(self._mode_index_from_code(target_mode))
        if not ok:
            self._sync_internal_short_button(self.current_mode == 3)

    def apply_mode(self):
        self._switch_frontend_mode(self.mode_combo.currentIndex())

    def change_pga(self, text: str):
        try:
            new_gain = int(text)
        except ValueError:
            return
        if new_gain not in VALID_GAINS:
            return
        self.gain = new_gain
        self.channel_gains[:] = new_gain
        self.lsb_uv = self.calc_lsb_uv()
        self.refresh_channel_parameter_labels()
        if self.transport_connected() and self.offline_uv is None:
            was_streaming = bool(self.streaming)
            try:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    time.sleep(0.08)
                self.transport_reset_input_buffer()
                self.transport_write(str(new_gain).encode("ascii"))
                time.sleep(0.08)
                self.transport_reset_input_buffer()
                if was_streaming:
                    self.transport_write(b"b")
                    self.streaming = True
                self.ring.clear()
                self.reset_processing_state()
                self.last_seq = None
                self.set_status(f"已发送 PGA={new_gain}；显示 LSB 同步为 {self.lsb_uv:.6g} uV/code。")
            except Exception as exc:
                self.streaming = False
                self.set_status(f"PGA 指令发送失败：{exc}")
        else:
            self.set_status(f"仅修改本地解码 PGA={new_gain}，LSB={self.lsb_uv:.6g} uV/code。")

    def _ble_pending_bytes(self) -> int:
        queued = len(self.ble_rx_buffer)
        worker = self.ble_worker
        if worker is not None:
            try:
                queued += worker.queued_data_bytes()
            except Exception:
                pass
        return int(queued)

    def _schedule_transport_repoll(self):
        """Continue draining a RAM backlog without monopolizing Qt."""
        if self._transport_repoll_pending or not self.transport_connected():
            return
        self._transport_repoll_pending = True
        delay = SERIAL_REPOLL_DELAY_MS if self.active_transport == "serial" else TRANSPORT_REPOLL_DELAY_MS
        QtCore.QTimer.singleShot(int(delay), self._run_transport_repoll)

    def _run_transport_repoll(self):
        self._transport_repoll_pending = False
        self.poll_transport()

    def poll_transport(self):
        """Consume host-side queues; OS I/O itself never runs on the Qt thread.

        USB bytes are continuously drained by SerialTransportWorker. BLE bytes
        are continuously reassembled by BleTransportWorker. This method only
        performs bounded parser/timeline work, then yields back to Qt.
        """
        if self._poll_serial_busy or not self.transport_connected():
            return
        if self.active_transport == "serial" and self.serial_control_read_active:
            return
        self._poll_serial_busy = True
        turn_started = time.perf_counter()
        remaining = 0
        try:
            if self.active_transport == "serial":
                data = b""
                if self.serial_worker is not None:
                    queued_before = self.serial_worker.queued_data_bytes()
                    self.transport_peak_pending_bytes = max(
                        self.transport_peak_pending_bytes, int(queued_before)
                    )
                    data = self.serial_worker.drain_data(SERIAL_MAX_PROCESS_BYTES)
                if data:
                    self.enqueue_raw_bytes(data)
                    frames = self.parser.feed(data)
                    if frames:
                        self.process_frames(frames, live=True)
                remaining = (
                    self.serial_worker.queued_data_bytes()
                    if self.serial_worker is not None else 0
                )
                self.last_serial_waiting_bytes = int(remaining)
            else:
                pending_before = self._ble_pending_bytes()
                self.transport_peak_pending_bytes = max(
                    self.transport_peak_pending_bytes, int(pending_before)
                )

                drained = b""
                if self.ble_worker is not None:
                    drained = self.ble_worker.drain_data(BLE_MAX_PROCESS_BYTES)
                if drained:
                    if not self.ble_rx_buffer:
                        self.ble_batch_started_monotonic = time.monotonic()
                    self.ble_rx_buffer.extend(drained)
                    self.enqueue_raw_bytes(drained)

                staged = len(self.ble_rx_buffer)
                worker_pending = 0
                if self.ble_worker is not None:
                    try:
                        worker_pending = self.ble_worker.queued_data_bytes()
                    except Exception:
                        worker_pending = 0
                held_s = (
                    0.0 if self.ble_batch_started_monotonic is None
                    else max(0.0, time.monotonic() - self.ble_batch_started_monotonic)
                )
                should_process = bool(
                    staged >= BLE_COALESCE_MIN_BYTES
                    or (staged > 0 and held_s >= BLE_COALESCE_MAX_HOLD_S)
                    or staged >= BLE_MAX_PROCESS_BYTES
                )

                if should_process:
                    take = min(staged, BLE_MAX_PROCESS_BYTES)
                    data = bytes(self.ble_rx_buffer[:take])
                    del self.ble_rx_buffer[:take]
                    if self.ble_rx_buffer:
                        self.ble_batch_started_monotonic = time.monotonic()
                    else:
                        self.ble_batch_started_monotonic = None
                    frames = self.parser.feed(data)
                    if frames:
                        self.ble_coalesced_batches += 1
                        self.process_frames(frames, live=True)

                remaining = int(len(self.ble_rx_buffer) + worker_pending)
                self.last_serial_waiting_bytes = remaining
        except Exception as exc:
            self.set_status(f"{self.transport_description()} 读取异常：{exc}")
        finally:
            if self.active_transport == "serial" and self.serial_worker is not None:
                remaining = self.serial_worker.queued_data_bytes()
            elif self.active_transport == "ble":
                remaining = self._ble_pending_bytes()
            self.last_serial_waiting_bytes = int(remaining)
            self.transport_peak_pending_bytes = max(
                self.transport_peak_pending_bytes, int(remaining)
            )
            if self.live_sample_count:
                self.live_lag_s = max(
                    float(self.last_pending) / FS,
                    float(self.last_queue_depth) / FS,
                    float(remaining) / BYTES_PER_SECOND,
                )
            turn_ms = (time.perf_counter() - turn_started) * 1000.0
            self.transport_last_turn_ms = turn_ms
            self.transport_max_turn_ms = max(self.transport_max_turn_ms, turn_ms)
            self._poll_serial_busy = False

        if remaining > 0 and self.transport_connected():
            needs_repoll = True
            if self.active_transport == "ble":
                worker_pending = 0
                if self.ble_worker is not None:
                    try:
                        worker_pending = self.ble_worker.queued_data_bytes()
                    except Exception:
                        worker_pending = 0
                held_s = (
                    0.0 if self.ble_batch_started_monotonic is None
                    else max(0.0, time.monotonic() - self.ble_batch_started_monotonic)
                )
                needs_repoll = bool(
                    worker_pending > 0
                    or len(self.ble_rx_buffer) >= BLE_COALESCE_MIN_BYTES
                    or held_s >= BLE_COALESCE_MAX_HOLD_S
                )
            if needs_repoll:
                self._schedule_transport_repoll()
    def poll_serial(self):
        """Backward-compatible alias used by older scripts/tests."""
        self.poll_transport()

    def process_frames(self, frames: List[Frame], live: bool):
        if not frames:
            return
        now = time.perf_counter()
        detected_reference = None
        previous_sequence = self.last_seq
        previous_mode = int(self.current_mode)
        use_ble_timeline = bool(live and self.active_transport == "ble")

        if use_ble_timeline:
            (
                timeline_values,
                timeline_valid,
                timeline_sequence,
                timeline_modes,
                _lost_samples,
                filled_samples,
                gap_events,
                large_discontinuities,
                _last_timeline_seq,
                _last_timeline_mode,
            ) = expand_frames_to_timeline(
                frames,
                previous_sequence=previous_sequence,
                previous_mode=previous_mode,
            )
            self.timeline_gap_samples += int(filled_samples)
            self.timeline_gap_events += int(gap_events)
            self.timeline_large_discontinuities += int(large_discontinuities)
        elif live:
            # Proven serial behavior: append only bytes actually received.  Do
            # not allocate NaN columns on the hot USB path; sequence accounting
            # remains exact in the loop below.
            timeline_values = np.stack([fr.uv for fr in frames], axis=1).astype(np.float32)
            timeline_valid = np.array([fr.valid for fr in frames], dtype=bool)
            timeline_sequence = np.array([fr.sequence for fr in frames], dtype=np.uint32)
            timeline_modes = np.array([fr.mode for fr in frames], dtype=np.uint8)
            large_discontinuities = 0
        else:
            timeline_values = timeline_valid = timeline_sequence = timeline_modes = None
            large_discontinuities = 0

        for fr in frames:
            self.packet_count += 1
            if not (fr.flags & 0x01):
                self.status_bad += 1
            if not (fr.flags & 0x02):
                self.drdy_bad += 1
            if (fr.flags & 0x04) or fr.pending > 1:
                self.backlog_events += 1

            drop_delta = (fr.queue_drop_low - self.last_queue_drop_low) & 0xFF
            if self.packet_count > 1 and 0 < drop_delta < 128:
                self.queue_drop_hints += drop_delta
            self.last_queue_drop_low = fr.queue_drop_low

            gap = sequence_gap_size(self.last_seq, fr.sequence)
            if gap:
                self.seq_lost += gap
                self.seq_gap_events += 1
                # pending/backlog and queue-drop counters are generated inside
                # the C3.  Without either hint, the most likely loss point is
                # host USB/BLE reception or parser resynchronisation.
                if fr.pending > 1 or bool(fr.flags & 0x04) or (0 < drop_delta < 128):
                    self.seq_device_lost += gap
                else:
                    self.seq_host_lost += gap

            if self.last_seq is None:
                self.first_seq = fr.sequence
                self.first_clock = now
            self.last_seq = fr.sequence
            if live and self.first_clock is not None and self.first_seq is not None:
                elapsed = now - self.first_clock
                if elapsed > 1:
                    progressed = ((fr.sequence - self.first_seq) & 0xFFFFFFFF) + 1
                    self.fs_est = progressed / elapsed

            frame_saturated = np.abs(fr.raw_counts) >= (
                ADC_SATURATION_FRACTION * (2**23 - 1)
            )
            enabled_saturated = frame_saturated & self.channel_enabled
            self.saturation_samples += int(np.sum(enabled_saturated))
            self.saturation_channel_samples += enabled_saturated.astype(np.int64)
            self.current_mode = fr.mode
            if fr.mode in (0, 1, 2):
                detected_reference = (
                    REFERENCE_SRB1 if (fr.flags & 0x80) else REFERENCE_SRB2
                )
            self.last_read_us = fr.read_us
            self.max_read_us = max(self.max_read_us, fr.read_us)
            self.last_pending = fr.pending
            self.last_queue_depth = fr.queue_depth

        if live:
            self.live_sample_count += len(frames)
            timeline_count = int(timeline_values.shape[1])
            self.live_timeline_sample_count += timeline_count
            self.live_lag_s = max(
                float(self.last_pending) / FS,
                float(self.last_queue_depth) / FS,
                float(self.last_serial_waiting_bytes) / BYTES_PER_SECOND,
            )
            self.ring.append_batch(
                timeline_values, timeline_valid, timeline_sequence, timeline_modes
            )
            if self.stream_server is not None and timeline_count:
                self.stream_server.set_first_sequence(int(timeline_sequence[0]))
            self._publish_stream_batch(
                STREAM_RAW,
                timeline_values,
                timeline_valid,
                timeline_sequence,
                timeline_modes,
                generation=None,
            )
            # Raw ring/BIN keep the exact ADC rail values. For the live filter
            # and screen copy only, isolate rail samples per channel with NaN.
            # Otherwise a floating BIAS/electrode can alternate between +/-FS and
            # force pyqtgraph to draw thousands of full-height vertical segments,
            # starving the BLE decoder/ACK path even though radio throughput is OK.
            filter_values = np.asarray(timeline_values, dtype=np.float32).copy()
            saturation_matrix = self.saturation_mask_uv(filter_values)
            if np.any(saturation_matrix):
                filter_values[saturation_matrix] = np.nan
            if self.filter_worker is not None:
                self.filter_worker.submit(
                    self.filter_generation,
                    filter_values,
                    timeline_valid,
                    timeline_sequence,
                    timeline_modes,
                )
            else:
                self.append_live_filtered(
                    frames,
                    filter_values,
                    timeline_valid,
                    timeline_sequence,
                    timeline_modes,
                )
            if use_ble_timeline and large_discontinuities:
                self.display_cursor_sample = None
                self.display_buffer_started = False
                self.display_buffer_state = "priming"

        if detected_reference is not None and detected_reference != self.reference_mode:
            self.set_reference_mode_local(detected_reference)
        self._sync_internal_short_button(self.current_mode == 3)
    def drain_filter_results(self):
        worker = self.filter_worker
        if worker is None:
            return
        deadline = time.perf_counter() + FILTER_RESULT_BUDGET_S
        for batch in worker.drain(max_batches=32):
            if batch.generation != self.filter_generation:
                self.filter_stale_batches += 1
                continue
            self.filtered_ring.append_batch(
                batch.filtered, batch.valid, batch.sequence, batch.modes
            )
            self._publish_stream_batch(
                STREAM_FILTERED,
                batch.filtered,
                batch.valid,
                batch.sequence,
                batch.modes,
                generation=batch.generation,
            )
            self.filter_batches_applied += 1
            if time.perf_counter() >= deadline:
                break

    def _publish_stream_batch(
        self,
        stream: str,
        values: np.ndarray,
        valid: np.ndarray,
        sequence: np.ndarray,
        modes: np.ndarray,
        generation: Optional[int],
    ) -> None:
        server = self.stream_server
        if server is None:
            return
        try:
            publish_gui_matrix(
                server,
                stream=stream,
                values=values,
                sequence=sequence,
                valid=valid,
                modes=modes,
                generation=generation,
                session_id=server.session_id,
            )
        except Exception as exc:
            self.stream_api_errors += 1
            if self.stream_api_errors == 1:
                print(f"Local EEG stream publish disabled: {exc}", file=sys.stderr)
                self.stream_server = None

    def filter_worker_backlog_samples(self) -> int:
        worker = self.filter_worker
        if worker is None:
            return 0
        metrics = worker.metrics()
        return int(metrics["queued_samples"] + metrics["output_samples"])

    def current_bias_mask(self) -> int:
        mask = 0
        for i, cb in enumerate(self.bias_checks):
            if cb.isChecked() and self.channel_enabled[i]:
                mask |= (1 << i)
        return mask & 0xFF

    def update_bias_mask_label(self):
        self.bias_mask_label.setText(f"mask=0x{self.current_bias_mask():02X}")

    def set_bias_checks(self, mask: int):
        enabled_mask = sum(
            (1 << i) for i in range(CHANNELS) if self.channel_enabled[i]
        )
        mask &= enabled_mask
        for i, cb in enumerate(self.bias_checks):
            cb.blockSignals(True)
            cb.setChecked(bool(mask & (1 << i)))
            cb.blockSignals(False)
        self.update_bias_mask_label()

    def apply_bias_sensp(self):
        mask = self.current_bias_mask()
        if not self.require_transport():
            return
        was_streaming = bool(self.streaming)
        try:
            if was_streaming:
                self.transport_write(b"s")
                self.streaming = False
                time.sleep(0.12)
            self.transport_reset_input_buffer()
            self.transport_write(bytes([0xA6, 0x0D, mask]))
            ack = self.read_config_ack(0xA6, expected_argument=mask & 0xFF)
            if ack is None:
                raise RuntimeError(
                    "固件未返回 A6 寄存器读回。请烧录带配置 ACK 的最新固件。"
                )
            if ack["argument"] != mask or not ack["verified"]:
                raise RuntimeError(
                    f"ADS1299 BIAS 校验失败：请求=0x{mask:02X}, "
                    f"逻辑mask=0x{ack['channel_register']:02X}, "
                    f"P=0x{ack['bias_p']:02X}, N=0x{ack['bias_n']:02X}"
                )
            for i in range(CHANNELS):
                self.channel_bias[i] = bool(mask & (1 << i))
            self.set_bias_checks(mask)
            self.refresh_channel_parameter_labels()
            self.set_status(
                f"BIAS 写入已由 ADS1299 读回确认：逻辑 mask=0x{mask:02X}，"
                f"BIAS_SENSP=0x{ack['bias_p']:02X}，"
                f"BIAS_SENSN=0x{ack['bias_n']:02X}，"
                f"MISC1=0x{ack['misc1']:02X}。"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "BIAS 写入/校验失败", str(exc))
        finally:
            if was_streaming and self.transport_connected():
                try:
                    self.transport_reset_input_buffer()
                    self.transport_write(b"b")
                    self.streaming = True
                except Exception:
                    self.streaming = False

    # ---------------- bin import/offline ----------------
    def _finish_offline_load(self, path: str):
        """Update navigation and plots after any supported file is loaded."""
        self.offline_end = self.offline_uv.shape[1]
        self.offline_slider.setEnabled(True)
        self.offline_slider.setRange(1, self.offline_end)
        self.offline_slider.setValue(self.offline_end)
        self.offline_label.setText(
            f"{Path(path).name}: {self.offline_end / FS:.1f}s"
        )
        if hasattr(self, "file_status"):
            self.file_status.setText(
                f"{Path(path).name}  |  {FS} Hz  |  "
                f"有效采样 {int(np.sum(self.offline_valid))}/{self.offline_end}"
            )
        self.update_fast_plots()
        self.update_psd_and_info()

    def _load_bin_path(self, path: str):
        """Load one raw BIN into the shared offline/export data model."""
        raw = Path(path).read_bytes()
        parser = AdsFrameParser(self.channel_lsb_uv)
        frames = parser.feed(raw)
        if not frames:
            raise RuntimeError("没有解析出有效 48-byte 帧。")
        self.reset_processing_state()
        (
            expanded_uv,
            expanded_valid,
            expanded_seq,
            expanded_mode,
            _lost,
            _filled,
            _events,
            _large,
            _last_seq,
            _last_mode,
        ) = expand_frames_to_timeline(
            frames,
            previous_sequence=None,
            previous_mode=int(frames[0].mode),
        )
        self.offline_uv = expanded_uv.astype(np.float32, copy=False)
        self.loaded_path = str(path)
        self.offline_valid = expanded_valid
        self.offline_seq = expanded_seq
        self.offline_mode = expanded_mode
        self.current_mode = int(self.offline_mode[-1])
        if self.current_mode in (0, 1, 2):
            self.set_reference_mode_local(
                REFERENCE_SRB1 if (frames[-1].flags & 0x80) else REFERENCE_SRB2
            )
        self._finish_offline_load(path)
        return parser

    def _load_bdf_path(self, path: str):
        """Load a BDF/BDF+ file into the GUI's common 8-channel data model."""
        try:
            import mne
        except ImportError as exc:
            raise RuntimeError("读取 BDF 需要 MNE，请先安装 requirements.txt 中的依赖。") from exc

        raw = mne.io.read_raw_bdf(path, preload=True, verbose="ERROR")
        if not raw.ch_names:
            raise RuntimeError("BDF 文件中没有可读取的信号通道。")

        # Prefer physiological channels and fall back to every non-stim channel.
        picks = mne.pick_types(
            raw.info, eeg=True, eog=True, ecg=True, emg=True,
            misc=True, stim=False, exclude=[],
        )
        if not len(picks):
            picks = np.array(
                [i for i, kind in enumerate(raw.get_channel_types()) if kind != "stim"],
                dtype=int,
            )
        if not len(picks):
            raise RuntimeError("BDF 文件中没有可用的数据通道。")

        raw.pick(picks[:CHANNELS])
        source_sfreq = float(raw.info["sfreq"])
        if not np.isclose(source_sfreq, FS):
            raw.resample(FS, npad="auto", verbose="ERROR")

        data_uv = raw.get_data().astype(np.float64) * 1e6
        source_channels = data_uv.shape[0]
        if source_channels < CHANNELS:
            data_uv = np.pad(
                data_uv, ((0, CHANNELS - source_channels), (0, 0)),
                mode="constant",
            )
        if data_uv.shape[1] == 0:
            raise RuntimeError("BDF 文件不包含采样数据。")

        self.reset_processing_state()
        self.offline_uv = data_uv.astype(np.float32)
        self.loaded_path = str(path)
        sample_count = self.offline_uv.shape[1]
        self.offline_valid = np.ones(sample_count, dtype=bool)
        self.offline_seq = np.arange(sample_count, dtype=np.uint32)
        self.offline_mode = np.zeros(sample_count, dtype=np.uint8)
        self.current_mode = 0
        self._finish_offline_load(path)
        return source_channels, source_sfreq

    def import_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "导入文件",
            "",
            "支持的文件 (*.bin *.bdf);;ADS1299 BIN (*.bin);;BDF/BDF+ (*.bdf);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            self.stop_stream()
            if Path(path).suffix.lower() == ".bdf":
                channel_count, source_sfreq = self._load_bdf_path(path)
                resample_note = (
                    "" if np.isclose(source_sfreq, FS)
                    else f"，已从 {source_sfreq:g} Hz 重采样到 {FS} Hz"
                )
                pad_note = (
                    "" if channel_count == CHANNELS
                    else f"，读取 {channel_count} 个通道并补齐为 {CHANNELS} 通道"
                )
                self.set_status(
                    f"已导入 BDF：{path}{resample_note}{pad_note}，"
                    f"共 {self.offline_uv.shape[1]} 个采样点。"
                )
                return
            if Path(path).suffix.lower() != ".bin":
                raise RuntimeError("不支持该文件格式，请选择 .bin 或 .bdf 文件。")
            parser = self._load_bin_path(path)
            valid_count = int(np.sum(self.offline_valid))
            total_count = int(self.offline_uv.shape[1])
            self.set_status(
                f"已导入 {path}，有效帧 {valid_count}/{total_count}，"
                f"CRC坏帧 {parser.crc_bad}。"
            )
            if QtWidgets.QMessageBox.question(
                self,
                "转换采集文件",
                "BIN 已导入。是否现在转换为 BDF 或 MNE FIF？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            ) == QtWidgets.QMessageBox.Yes:
                self.export_biosignal_formats()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "导入失败", str(exc))

    def import_bin(self):
        """Backward-compatible entry point for older callers."""
        self.import_file()

    def offline_slider_changed(self, value: int):
        self.offline_end = int(value)
        self.reset_psd_smoothing()
        if self.offline_uv is not None:
            self.offline_label.setText(f"{self.offline_end/FS:.1f}/{self.offline_uv.shape[1]/FS:.1f}s")
        self.update_fast_plots()

    def reset_display_jitter_buffer(self):
        self.display_cursor_sample = None
        self.display_last_tick = time.monotonic()
        self.display_target_delay_samples = int(round(DISPLAY_JITTER_BASE_TARGET_S * FS))
        self.display_startup_samples = int(round(DISPLAY_JITTER_STARTUP_S * FS))
        self.display_buffer_started = False
        self.display_buffer_state = "priming"
        self.display_buffer_underruns = 0
        self.display_low_latency_resyncs = 0
        self.display_rebuffer_events = 0
        self.display_rebuffer_started_at = None
        self.display_rebuffer_last_s = 0.0
        self.display_rebuffer_max_s = 0.0
        self.display_delay_s = 0.0
        self.display_reserve_samples = 0
        self.display_last_end_sample = -1
        self.render_gap_last_ms = 0.0
        self.render_gap_max_ms = 0.0
        self.render_gap_over_100ms = 0
        self._last_render_monotonic = None

    def update_adaptive_display_target(self):
        """Grow the reserve when this Windows link demonstrates larger gaps.

        The target never shrinks during a recording, avoiding cursor jumps and
        preserving every sample in display order. It is capped at 1 s so one OS
        suspension cannot create permanently growing live-view latency.
        """
        if self.active_transport != "ble" or self.ble_worker is None:
            return
        try:
            _last_gap, max_gap, _burst, _long_count = self.ble_worker.timing_metrics()
        except Exception:
            return
        requested_s = min(
            DISPLAY_JITTER_MAX_TARGET_S,
            max(DISPLAY_JITTER_BASE_TARGET_S, float(max_gap) + DISPLAY_JITTER_MARGIN_S),
        )
        requested_samples = int(round(requested_s * FS))
        if requested_samples > self.display_target_delay_samples:
            self.display_target_delay_samples = requested_samples

    def advance_live_display_cursor(self) -> int:
        """Advance the live-only cursor behind an adaptive reserve.

        Normal playback preserves order and can temporarily run faster than real
        time to remove burst-induced lag. After an unusually long OS stall, only
        stale screen history may be skipped; raw BIN and analysis rings remain
        complete.
        """
        total = int(self.ring.total_appended)
        now = time.monotonic()
        dt = max(0.0, min(DISPLAY_JITTER_MAX_DT_S, now - self.display_last_tick))
        self.display_last_tick = now
        self.update_adaptive_display_target()
        target = max(self.display_startup_samples, self.display_target_delay_samples)

        if self.display_cursor_sample is None:
            self.display_reserve_samples = total
            self.display_delay_s = total / FS
            if total < target:
                self.display_buffer_state = "priming"
                return 0
            self.display_cursor_sample = float(max(0, total - target))
            self.display_buffer_started = True
            self.display_buffer_state = "playing"

        cursor = float(self.display_cursor_sample)
        reserve = max(0.0, float(total) - cursor)

        # Older builds entered a full "rebuffering" stop here and waited until
        # the whole reserve had refilled. For EEG that looks like an application
        # freeze. V16 continuity mode never waits for a full refill: as soon as
        # even a few new samples arrive, playback continues.
        if self.display_buffer_state == "rebuffering":
            self.display_buffer_state = "playing"
            self.display_rebuffer_started_at = None

        # Keep the screen close to real time without touching the lossless raw
        # and filtered rings. A delayed Windows notification burst can increase
        # reserve abruptly; accelerate the playback cursor smoothly to remove
        # that excess. Very stale screen history is skipped only on the display
        # path so live latency never ratchets upward for the rest of a recording.
        reserve = max(0.0, float(total) - cursor)
        hard_max = int(round(DISPLAY_JITTER_HARD_MAX_S * FS))
        hard_trigger = hard_max + int(round(DISPLAY_JITTER_HARD_HYSTERESIS_S * FS))
        if reserve > hard_trigger:
            cursor = float(max(0, total - target))
            self.display_cursor_sample = cursor
            self.display_low_latency_resyncs += 1
            reserve = max(0.0, float(total) - cursor)

        desired_advance = dt * FS
        excess = max(0.0, reserve - float(target))
        catchup_trigger = DISPLAY_JITTER_CATCHUP_TRIGGER_S * FS
        if excess > catchup_trigger and desired_advance > 0.0:
            ramp_span = max(1.0, 0.50 * FS)
            fraction = min(1.0, (excess - catchup_trigger) / ramp_span)
            rate = 1.0 + (DISPLAY_JITTER_CATCHUP_MAX_RATE - 1.0) * fraction
            desired_advance *= rate
        elif reserve < float(target) and desired_advance > 0.0:
            # Do not freeze when Windows delivers BLE in bursts. As the reserve
            # gets low, smoothly slow visual playback instead of stopping and
            # waiting for a full buffer refill. Accepted latency remains <1 s.
            ratio = max(0.0, min(1.0, reserve / max(1.0, float(target))))
            rate = DISPLAY_JITTER_LOW_RESERVE_RATE + (1.0 - DISPLAY_JITTER_LOW_RESERVE_RATE) * ratio
            desired_advance *= rate

        max_end = float(max(0, total - self.display_min_reserve_samples))
        available_advance = max(0.0, max_end - cursor)
        if desired_advance > 0.5 and available_advance + 1e-9 < desired_advance:
            cursor += available_advance
            self.display_cursor_sample = max(0.0, cursor)
            self.display_buffer_underruns += 1
            self.display_rebuffer_events += 1
            self.display_buffer_state = "starved"
        else:
            cursor = min(cursor + desired_advance, max_end)
            self.display_cursor_sample = max(0.0, cursor)
            self.display_buffer_state = "playing"

        end_sample = int(self.display_cursor_sample)
        self.display_reserve_samples = max(0, total - end_sample)
        self.display_delay_s = self.display_reserve_samples / FS
        return end_sample

    def get_live_window_ending_at(
        self, source: RingBuffer, n: int, end_sample: int
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return a live window ending at an absolute session sample count."""
        total = int(source.total_appended)
        end_sample = int(np.clip(end_sample, 0, total))
        lag = max(0, total - end_sample)
        request = min(source.count, max(0, int(n) + lag))
        data, valid, seq, mode = source.latest(request)
        if lag > 0:
            keep_end = max(0, data.shape[1] - lag)
            data = data[:, :keep_end]
            valid = valid[:keep_end]
            seq = seq[:keep_end]
            mode = mode[:keep_end]
        if data.shape[1] > n:
            data = data[:, -n:]
            valid = valid[-n:]
            seq = seq[-n:]
            mode = mode[-n:]
        return data, valid, seq, mode

    def get_view_data(
        self, seconds: float, filtered_live: bool = False,
        live_end_sample: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        n = max(1, int(seconds * FS))
        if self.offline_uv is not None:
            end = max(1, min(self.offline_end, self.offline_uv.shape[1]))
            start = max(0, end - n)
            return (
                self.offline_uv[:, start:end].copy(),
                self.offline_valid[start:end].copy(),
                self.offline_seq[start:end].copy(),
                self.offline_mode[start:end].copy(),
            )
        source = self.filtered_ring if filtered_live else self.ring
        if live_end_sample is None:
            return source.latest(n)
        return self.get_live_window_ending_at(source, n, int(live_end_sample))

    def get_psd_data(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int]:
        """Return an offline interval of at least 10 s around the visible view."""
        if self.offline_uv is not None:
            total = self.offline_uv.shape[1]
            visible_start = int(round(float(self.start_time_spin.value()) * FS))
            visible_length = max(1, int(round(float(self.win_spin.value()) * FS)))
            visible_start = max(0, min(visible_start, max(0, total - 1)))
            visible_end = min(total, visible_start + visible_length)

            # Keep waveform zoom independent from spectral resolution. Center
            # a minimum ten-second PSD interval on the visible view, and shift
            # it at file boundaries to preserve its full length when possible.
            analysis_length = min(total, max(10 * FS, visible_end - visible_start))
            center = (visible_start + visible_end) // 2
            start = center - analysis_length // 2
            start = max(0, min(start, total - analysis_length))
            end = start + analysis_length
            return (
                self.offline_uv[:, start:end].copy(),
                self.offline_valid[start:end].copy(),
                self.offline_seq[start:end].copy(),
                self.offline_mode[start:end].copy(),
                start,
                end,
            )
        data, valid, seq, mode = self.get_view_data(PSD_LIVE_WINDOW_S, filtered_live=False)
        end = int(self.ring.total_appended)
        start = max(0, end - data.shape[1])
        return data, valid, seq, mode, start, end

    # ---------------- signal processing/plotting ----------------
    def prepare_plot_signal(self, x: np.ndarray, valid: np.ndarray, filtered_live: bool) -> np.ndarray:
        x = np.asarray(x, dtype=float).copy()
        # Saturation is a quality condition, not an acquisition stop condition.
        # Filtered-live data is already channel-masked by the filter worker.
        if filtered_live:
            y = x
        elif self.filter_check.isChecked():
            y = self.filter_offline_display(x, valid)
        else:
            y, _ok, _gap = self.clean_with_valid(x, valid, max_gap=2)
            y = signal.detrend(y, type="constant")
        if valid.size == y.size:
            y = y.copy()
            y[~valid] = np.nan
        return y

    def update_legacy_fast_plots(self):
        seconds = float(self.win_spin.value())
        live_filtered = bool(self.filter_check.isChecked() and self.offline_uv is None)
        data, valid, _seq, _mode = self.get_view_data(seconds, filtered_live=live_filtered)
        if data.shape[1] < 2:
            return
        ch = self.channel_combo.currentIndex()
        y = self.prepare_plot_signal(data[ch], valid, filtered_live=live_filtered)
        t = (np.arange(y.size) - y.size + 1) / FS
        self.time_curve.setData(t, y)
        self.time_plot.setXRange(float(t[0]), float(t[-1]), padding=0)
        yr = float(self.yrange_spin.value())
        if yr > 0:
            self.time_plot.setYRange(-yr, yr, padding=0)
        else:
            finite = y[np.isfinite(y)]
            if finite.size:
                med = float(np.median(finite))
                half = float(max(10.0, np.percentile(np.abs(finite - med), 99.5) * 1.2))
                self.time_plot.setYRange(med - half, med + half, padding=0)
        chain = "连续5-50Hz+50/100Hz谐波陷波" if self.filter_check.isChecked() else "原始去均值"
        self.time_plot.setTitle(
            f"{self.channel_combo.currentText()} 时域 | {MODE_NAMES.get(self.current_mode, 'UNKNOWN')} | {chain}"
        )

        # Stack plot uses at most 5 s to remain snappy.
        stack_data, stack_valid, _stack_seq, _ = self.get_view_data(
            min(5.0, seconds), filtered_live=live_filtered
        )
        if stack_data.shape[1] < 2:
            return
        stack_t = (np.arange(stack_data.shape[1]) - stack_data.shape[1] + 1) / FS
        arr = np.vstack([
            self.prepare_plot_signal(stack_data[c], stack_valid, filtered_live=live_filtered)
            for c in range(CHANNELS)
        ])
        std = np.nanmedian(np.nanstd(arr, axis=1))
        spacing = float(max(50.0, 5.0 * std if np.isfinite(std) else 100.0))
        for c, curve in enumerate(self.stack_curves):
            curve.setData(stack_t, arr[c] + (CHANNELS - 1 - c) * spacing)
        self.stack_plot.setXRange(float(stack_t[0]), float(stack_t[-1]), padding=0)
        self.stack_plot.setYRange(-spacing, CHANNELS * spacing, padding=0.02)
        ticks = [((CHANNELS - 1 - c) * spacing, f"CH{c+1}") for c in range(CHANNELS)]
        self.stack_plot.getAxis("left").setTicks([ticks])

    def update_fast_plots(self, *_args):
        """Paint from the newest USB data or BLE's delayed jitter cursor."""
        if self._plot_update_busy:
            return

        now = time.monotonic()
        if self._last_render_monotonic is not None:
            gap_ms = max(0.0, (now - self._last_render_monotonic) * 1000.0)
            self.render_gap_last_ms = gap_ms
            self.render_gap_max_ms = max(self.render_gap_max_ms, gap_ms)
            if gap_ms >= 100.0:
                self.render_gap_over_100ms += 1
        self._last_render_monotonic = now

        is_live = self.offline_uv is None
        filter_backlog_s = self.filter_worker_backlog_samples() / FS
        if is_live and self.active_transport == "serial":
            # Receive and raw recording always win. Filtering has its own FIFO;
            # Qt paints only after that worker catches up.
            self.poll_transport()
            if (
                self.sender() is getattr(self, "plot_timer", None)
                and self.packet_count == self._last_live_plot_packet
            ):
                return
            # Never intentionally freeze the waveform because a background
            # queue is busy. Transport and BIN recording are already isolated.
            # Paint the newest processed history available on every timer tick.
            _effective_lag_s = max(self.live_lag_s, filter_backlog_s)

        if is_live and self.active_transport == "ble":
            ble_backlog = self._ble_pending_bytes()
            ble_backlog_s = ble_backlog / BYTES_PER_SECOND
            _effective_lag_s = max(ble_backlog_s, filter_backlog_s)

        use_jitter = bool(
            is_live and self.streaming and self.active_transport == "ble"
        )
        display_end = None
        if use_jitter:
            display_end = self.advance_live_display_cursor()
            if not self.display_buffer_started:
                if hasattr(self, "range_status"):
                    self.range_status.setText(
                        f"建立无线平滑缓冲：{self.display_reserve_samples/FS:.2f}/"
                        f"{self.display_target_delay_samples/FS:.2f} s；原始数据已正常保存"
                    )
                return
            if (
                self.sender() is getattr(self, "plot_timer", None)
                and display_end == self.display_last_end_sample
            ):
                return

        self._plot_update_busy = True
        try:
            self._render_fast_plots(display_end_sample=display_end)
            if is_live:
                self._last_live_plot_packet = self.packet_count
                if use_jitter:
                    self.display_last_end_sample = int(display_end or 0)
        except Exception as exc:
            # A transient plotting/array error must not terminate the Qt timer
            # callback chain and leave the window looking permanently frozen.
            self.plot_errors += 1
            self.set_status(f"绘图异常已隔离（采集和 BIN 保存继续）：{exc}")
        finally:
            self._plot_update_busy = False
    def _render_fast_plots(self, display_end_sample: Optional[int] = None):
        """Render eight independent channel plots with per-channel y-scales."""
        seconds = float(self.win_spin.value())
        total_n = self._total_samples()
        if total_n < 2:
            return
        total_s = total_n / FS
        if self.offline_uv is not None:
            start_s = min(float(self.start_time_spin.value()), max(0.0, total_s - seconds))
            start = int(start_s * FS)
            end = min(total_n, start + int(seconds * FS))
            data = self.offline_uv[:, start:end].copy()
            valid = self.offline_valid[start:end].copy()
            live_filtered = False
        else:
            live_filtered = bool(self.filter_check.isChecked())
            if display_end_sample is None:
                display_end_sample = int(total_n)
            display_end_sample = int(np.clip(display_end_sample, 0, total_n))
            data, valid, _seq, _mode = self.get_view_data(
                seconds, filtered_live=live_filtered,
                live_end_sample=display_end_sample,
            )
            start_s = max(0.0, (display_end_sample - data.shape[1]) / FS)
            self.start_time_spin.blockSignals(True)
            self.start_time_spin.setValue(start_s)
            self.start_time_spin.blockSignals(False)
        if data.shape[1] < 2:
            return
        raw_for_saturation = None
        if self.offline_uv is None and live_filtered:
            # The causal filter was already applied once when samples entered
            # filtered_ring. Never rerun zero-phase filtering on every repaint.
            arr = np.asarray(data, dtype=float)
            if valid.size == arr.shape[1] and not np.all(valid):
                arr = arr.copy()
                arr[:, ~valid] = np.nan
            raw_for_saturation, _rv, _rs, _rm = self.get_view_data(
                seconds, filtered_live=False, live_end_sample=display_end_sample
            )
        elif self.offline_uv is None:
            arr = np.asarray(data, dtype=float)
            if valid.size == arr.shape[1] and not np.all(valid):
                arr = arr.copy()
                arr[:, ~valid] = np.nan
            raw_for_saturation = np.asarray(data, dtype=float)
            with np.errstate(invalid="ignore"):
                channel_means = np.nanmean(arr, axis=1, keepdims=True)
            channel_means = np.nan_to_num(
                channel_means, nan=0.0, posinf=0.0, neginf=0.0
            )
            arr = arr - channel_means
        else:
            if self.filter_check.isChecked():
                arr = self.filter_offline_view(start, end)
            else:
                arr = np.vstack([
                    self.prepare_plot_signal(data[c], valid, False)
                    for c in range(CHANNELS)
                ])
            raw_for_saturation = np.asarray(data, dtype=float)

        # Mask only the screen copy. Rail-to-rail toggling otherwise creates a
        # dense vertical QPainter path like a solid rectangle and can starve the
        # GUI event loop. Raw ring/BIN and sequence counters are untouched.
        visible_saturation = np.zeros_like(arr, dtype=bool)
        if raw_for_saturation is not None and arr.ndim == 2:
            raw_for_saturation = np.asarray(raw_for_saturation, dtype=float)
            if raw_for_saturation.shape[1] >= arr.shape[1]:
                raw_for_saturation = raw_for_saturation[:, -arr.shape[1]:]
            else:
                pad = arr.shape[1] - raw_for_saturation.shape[1]
                raw_for_saturation = np.pad(
                    raw_for_saturation,
                    ((0, 0), (pad, 0)),
                    mode="constant",
                    constant_values=np.nan,
                )
            visible_saturation = self.saturation_mask_uv(raw_for_saturation)
            if self.offline_uv is None and np.any(visible_saturation):
                # Screen-only overload protection. A floating channel at the ADC
                # rails is intentionally not drawn point-to-point; doing so can
                # monopolize QPainter and starve BLE ACK scheduling. Raw ring,
                # raw BIN, timestamps and sequence numbers remain untouched.
                arr = np.asarray(arr, dtype=float).copy()
                arr[visible_saturation] = np.nan
        self.last_visible_saturated_channels = tuple(
            int(ch) for ch in np.flatnonzero(np.any(visible_saturation, axis=1))
        )
        n_plot = int(data.shape[1])
        t_rel = self._plot_time_cache.get(n_plot)
        if t_rel is None:
            t_rel = np.arange(n_plot, dtype=np.float64) / FS
            self._plot_time_cache[n_plot] = t_rel
        t = start_s + t_rel
        show_single = self.view_tabs.currentIndex() == self.single_tab_index
        single_ch = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
        single_scale = float(self.channel_scales[single_ch].value())
        if show_single:
            single_y = np.clip(np.asarray(arr[single_ch], dtype=float), -single_scale, single_scale)
            self.single_curve.setData(t, single_y)
            self.single_plot.setXRange(start_s, start_s + seconds, padding=0)
            single_key = (single_ch, single_scale)
            if self._last_single_y_range != single_key:
                self.single_plot.setYRange(-single_scale, single_scale, padding=0)
                self._last_single_y_range = single_key
        else:
            for c, curve in enumerate(self.stack_curves):
                scale = float(self.channel_scales[c].value())
                y_plot = np.clip(np.asarray(arr[c], dtype=float), -scale, scale)
                curve.setData(t, y_plot)
                if self._last_channel_y_ranges[c] != scale:
                    self.channel_plots[c].setYRange(-scale, scale, padding=0)
                    self._last_channel_y_ranges[c] = scale
        display_chain = (
            f"{self.hp_spin.value():g}–{self.lp_spin.value():g} Hz"
            + (" + 50/100 Hz harmonic notch" if self.notch_check.isChecked() else "")
            if self.filter_check.isChecked()
            else "原始数据（去直流）"
        )
        reference = (
            f"SRB2 {'ON' if self.channel_srb2[single_ch] else 'OFF'}"
            if self.reference_is_srb2()
            else "SRB1 GLOBAL"
        )
        single_title = (
            f"CH{single_ch+1} 独立波形 | {display_chain} | ±{single_scale:g} uV"
        )
        if single_title != self._last_single_title:
            self.single_plot.setTitle(single_title)
            self._last_single_title = single_title
        single_status = (
            f"CH{single_ch+1} | {'ON' if self.channel_enabled[single_ch] else 'OFF'}"
            f" | PGA ×{int(self.channel_gains[single_ch])}"
            f" | {'BIAS✓' if self.channel_bias[single_ch] else 'BIAS—'}"
            f" | {reference}"
        )
        if single_status != self._last_single_channel_status:
            self.single_channel_status.setText(single_status)
            self._last_single_channel_status = single_status
        if not show_single:
            self._syncing_plot = True
            # The remaining seven ViewBoxes are X-linked to the first.
            self.channel_plots[0].setXRange(start_s, start_s + seconds, padding=0)
            self._syncing_plot = False
        if self.offline_uv is not None:
            stride = max(1, total_n // 3000)
            overview = self.offline_uv[0, ::stride].astype(float)
            overview -= np.nanmean(overview)
            overview_t = np.arange(overview.size) * stride / FS
            self.nav_curve.setData(overview_t, overview)
            self.nav_plot.setXRange(0, total_s, padding=0)
            self.single_nav_curve.setData(overview_t, overview)
            self.single_nav_plot.setXRange(0, total_s, padding=0)
            self.single_nav_plot.setVisible(True)
        else:
            self.single_nav_plot.setVisible(False)
        now = time.monotonic()
        if self.offline_uv is not None or (
            not show_single and now - self._last_nav_update >= 0.2
        ):
            self._last_nav_update = now
            self._syncing_nav = True
            region = (start_s, min(total_s, start_s + seconds))
            self.nav_region.setRegion(region)
            self.single_nav_region.setRegion(region)
            self._syncing_nav = False
        hp, lp = self.hp_spin.value(), self.lp_spin.value()
        notch = " + 50/100 Hz harmonic notch" if self.notch_check.isChecked() else ""
        mode = f"{hp:g}–{lp:g} Hz{notch}" if self.filter_check.isChecked() else "原始数据（逐通道去直流）"
        if (
            self.offline_uv is None
            and self.active_transport == "ble"
            and self.streaming
            and self.display_buffer_started
        ):
            range_text = (
                f"{start_s:.1f}–{min(total_s,start_s+seconds):.1f} s | "
                f"无线缓冲 {self.display_delay_s*1000:.0f} ms"
            )
        else:
            range_text = f"{start_s:.1f}–{min(total_s,start_s+seconds):.1f} s"
        if self.last_visible_saturated_channels:
            channel_text = "/".join(
                f"CH{ch + 1}" for ch in self.last_visible_saturated_channels
            )
            range_text += f" | 饱和保护：{channel_text}（原始 BIN 保留）"
        if range_text != self._last_range_status_text:
            self.range_status.setText(range_text)
            self._last_range_status_text = range_text
        if mode != self._last_filter_status_text:
            self.filter_status.setText(mode)
            self._last_filter_status_text = mode

    def update_psd_and_info(self):
        """Request PSD work without blocking transport or plot painting."""
        filter_backlog_s = self.filter_worker_backlog_samples() / FS
        # PSD never back-pressures acquisition. It is single-flight already;
        # if the worker is busy, the current tick is simply ignored and the next
        # completed tick uses a fresh snapshot. No PSD request queue can grow.
        data, valid, seq, mode, analysis_start, analysis_end = self.get_psd_data()
        if data.shape[1] < FS * 4:
            self.latest_window_good = False
            self.psd_curve.setData([], [])
            self.latest_window_reason = "数据不足 4 秒"
            self.psd_plot.setTitle("Welch PSD | 等待至少 4 秒数据")
            self.update_info_text()
            return

        ch = self.channel_combo.currentIndex()
        channel_saturation = self.saturation_mask_uv(data)[ch]
        saturation_ratio = (
            float(np.mean(channel_saturation)) if channel_saturation.size else 0.0
        )
        # Saturation never stops PSD. It is only reported as signal quality.
        # The worker remains strictly single-flight, so expensive analysis can
        # never build a queue behind the BLE/plot path.
        signature = (
            bool(self.offline_uv is not None),
            int(analysis_start),
            int(analysis_end),
            int(ch),
            bool(self.psd_raw_check.isChecked()),
            int(self.psd_max_spin.value()),
        )
        if self.psd_worker_busy or signature == self.psd_last_signature:
            self.update_info_text()
            return

        self.psd_last_signature = signature
        self.psd_worker_busy = True
        request_id = self.psd_request_id
        self.psd_plot.setTitle("Welch PSD | 计算中…")
        worker = PsdWorker(
            self, request_id, data[ch], valid, seq, mode,
            self.sos_display_band.copy(), self.notch_check.isChecked(),
            live_fast=bool(self.offline_uv is None),
        )
        worker.signals.finished.connect(self.apply_psd_result)
        worker.signals.failed.connect(self.apply_psd_error)
        self.psd_pool.start(worker)
        self.update_info_text()

    @QtCore.Slot(int, object)
    def apply_psd_result(self, request_id: int, payload):
        self.psd_worker_busy = False
        if request_id != self.psd_request_id:
            return

        (good, reason, metrics), x, valid = payload
        self.latest_window_good = good
        self.latest_window_reason = reason
        self.latest_valid_ratio = float(np.mean(valid)) if valid.size else 0.0

        raw_mask = np.isfinite(x)
        if valid.size == x.size:
            raw_mask &= valid
        raw_samples = np.asarray(x[raw_mask], dtype=float)
        if raw_samples.size:
            raw_centered = raw_samples - np.mean(raw_samples)
            self.latest_raw_rms = float(np.sqrt(np.mean(raw_centered**2)))
            self.latest_raw_pp = float(np.ptp(raw_samples))
        else:
            self.latest_raw_rms = np.nan
            self.latest_raw_pp = np.nan
        self.latest_filtered_rms = metrics["filtered_rms"]

        raw_f = metrics["raw_f"]
        raw_p = metrics["raw_p"]
        display_f = metrics["display_f"]
        display_p = metrics["display_p"]
        alpha_f = metrics["alpha_f"]
        alpha_p = metrics["alpha_p"]
        if raw_f.size:
            line = (raw_f >= 48) & (raw_f <= 52)
            useful = (raw_f >= 5) & (raw_f <= 45)
            lp = float(np.trapezoid(raw_p[line], raw_f[line])) if np.any(line) else np.nan
            up = float(np.trapezoid(raw_p[useful], raw_f[useful])) if np.any(useful) else np.nan
            self.latest_line_ratio = lp / max(up, np.finfo(float).eps) if np.isfinite(lp) and np.isfinite(up) else np.nan

        if self.psd_raw_check.isChecked():
            plot_f, plot_p = raw_f, raw_p
            plot_name = "原始诊断 PSD"
        elif alpha_f.size:
            plot_f, plot_p = alpha_f, alpha_p
            plot_name = "滤波 PSD（对数域平滑）"
        else:
            plot_f, plot_p = display_f, display_p
            plot_name = "滤波 PSD（对数域平滑）"
        if plot_f.size:
            max_hz = float(self.psd_max_spin.value())
            smoothed_db = self.smooth_psd_db(plot_f, plot_p)
            mask = (plot_f >= 1) & (plot_f <= max_hz)
            self.psd_curve.setData(plot_f[mask], smoothed_db[mask])
            self.psd_plot.setXRange(1, max_hz, padding=0)

        self.latest_alpha_power = metrics["alpha_power"]
        self.latest_alpha_peak = metrics["alpha_peak"]
        self.latest_alpha_rel = metrics["alpha_rel"]
        self.advance_alpha_capture()

        peak_text = f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "---"
        rel_text = f"{100*self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
        self.psd_plot.setTitle(
            f"{plot_name} | Alpha 峰值 {peak_text} | Alpha rate {rel_text}"
        )
        self.update_info_text()

    @QtCore.Slot(int, str)
    def apply_psd_error(self, request_id: int, message: str):
        self.psd_worker_busy = False
        if request_id != self.psd_request_id:
            return
        self.psd_last_signature = None
        self.latest_window_good = False
        self.latest_window_reason = f"PSD 计算失败: {message}"
        self.psd_plot.setTitle(self.latest_window_reason)
        self.update_info_text()

    def store_alpha(self, closed: bool):
        kind = "closed" if closed else "open"
        label = "闭眼" if closed else "睁眼"
        if self.offline_uv is not None:
            value, used, reason = self.offline_alpha_median_20s()
            if not np.isfinite(value):
                QtWidgets.QMessageBox.warning(self, "Alpha", f"最近20秒没有足够合格窗口：{reason}")
                return
            if closed:
                self.closed_alpha = value
            else:
                self.open_alpha = value
            self.set_status(f"已保存离线最近20秒{label} Alpha 中位数，共 {used} 个合格窗口。")
            self.update_info_text()
            return

        if not self.streaming:
            QtWidgets.QMessageBox.warning(self, "Alpha", "请先开始实时采集，或导入一个 bin 文件。")
            return
        if self.current_mode in (3, 4):
            QtWidgets.QMessageBox.warning(self, "Alpha", "SHORTED/TEST 模式只做原始诊断，不采集 Alpha。")
            return
        self.alpha_capture_kind = kind
        self.alpha_capture_start = time.monotonic()
        self.alpha_capture_values = []
        self.open_btn.setEnabled(False)
        self.closed_btn.setEnabled(False)
        self.set_status(f"开始采集 {label} Alpha：保持状态 20 秒，坏窗口会自动丢弃。")

    def advance_alpha_capture(self):
        if self.alpha_capture_kind is None:
            return
        if self.latest_window_good and np.isfinite(self.latest_alpha_power):
            self.alpha_capture_values.append(float(self.latest_alpha_power))
        elapsed = time.monotonic() - self.alpha_capture_start
        label = "闭眼" if self.alpha_capture_kind == "closed" else "睁眼"
        if elapsed < 20.0:
            self.set_status(
                f"正在采集 {label} Alpha：{elapsed:.0f}/20 秒，已收 {len(self.alpha_capture_values)} 个合格窗口。"
            )
            return
        values = np.asarray(self.alpha_capture_values, dtype=float)
        if values.size >= 10:
            median_value = float(np.median(values))
            if self.alpha_capture_kind == "closed":
                self.closed_alpha = median_value
            else:
                self.open_alpha = median_value
            self.set_status(f"{label} Alpha 采集完成：{values.size} 个合格窗口，中位数已保存。")
        else:
            self.set_status(f"{label} Alpha 采集失败：20 秒内只有 {values.size} 个合格窗口。")
        self.alpha_capture_kind = None
        self.alpha_capture_values = []
        self.open_btn.setEnabled(True)
        self.closed_btn.setEnabled(True)

    def offline_alpha_median_20s(self) -> Tuple[float, int, str]:
        data, valid, seq, mode = self.get_view_data(20.0, filtered_live=False)
        if data.shape[1] < FS * 8:
            return np.nan, 0, "数据不足 8 秒"
        ch = self.channel_combo.currentIndex()
        window = FS * 4
        step = FS
        values: List[float] = []
        last_reason = "无合格窗口"
        for end in range(window, data.shape[1] + 1, step):
            start = end - window
            good, reason, metrics = self.compute_alpha_from_window(
                data[ch, start:end], valid[start:end], seq[start:end], mode[start:end]
            )
            last_reason = reason
            if good and np.isfinite(metrics["alpha_power"]):
                values.append(float(metrics["alpha_power"]))
        if len(values) < 5:
            return np.nan, len(values), last_reason
        return float(np.median(values)), len(values), "ok"

    def update_info_text(self):
        if (
            hasattr(self, "diagnostics_pause_btn")
            and self.diagnostics_pause_btn.isChecked()
        ):
            return
        now_diag = time.monotonic()
        if (
            self.streaming
            and (now_diag - getattr(self, "_last_diag_update_monotonic", 0.0)) < 0.50
        ):
            return
        self._last_diag_update_monotonic = now_diag
        selected_ch = self.channel_combo.currentIndex()
        selected_config = (
            f"CH{selected_ch+1} {'ON' if self.channel_enabled[selected_ch] else 'OFF'}, "
            f"PGA x{int(self.channel_gains[selected_ch])}, "
            f"{self.bias_register_name()}={'YES' if self.channel_bias[selected_ch] else 'NO'}, "
            + (
                f"SRB2={'ON' if self.channel_srb2[selected_ch] else 'OFF'}, SRB1=OFF"
                if self.reference_is_srb2()
                else "SRB1=GLOBAL, SRB2=OFF"
            )
        )
        fs_text = f"{self.fs_est:.2f}" if np.isfinite(self.fs_est) else "---"
        alpha_peak = f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "---"
        alpha_rel = f"{100*self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
        raw_rms = f"{self.latest_raw_rms:.2f} uV" if np.isfinite(self.latest_raw_rms) else "---"
        filtered_rms = f"{self.latest_filtered_rms:.2f} uV" if np.isfinite(self.latest_filtered_rms) else "---"
        raw_pp = f"{self.latest_raw_pp:.2f} uV" if np.isfinite(self.latest_raw_pp) else "---"
        line_ratio = f"{self.latest_line_ratio:.3f}" if np.isfinite(self.latest_line_ratio) else "---"
        valid_ratio = f"{100*self.latest_valid_ratio:.2f}%" if np.isfinite(self.latest_valid_ratio) else "---"
        saturation = 100 * self.saturation_samples / max(1, self.packet_count * 5)
        mode = MODE_NAMES.get(self.current_mode, "UNKNOWN")
        verdict = self.make_verdict(saturation)
        raw_path = self.raw_path if self.raw_path else "---"
        offline = "yes" if self.offline_uv is not None else "no"
        quality = "PASS" if self.latest_window_good else f"REJECT ({self.latest_window_reason})"
        capture = "none"
        if self.alpha_capture_kind is not None:
            capture = f"{self.alpha_capture_kind}, {time.monotonic()-self.alpha_capture_start:.0f}/20s, n={len(self.alpha_capture_values)}"
        comparison = "---"
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            comparison = f"{10*np.log10(max(self.closed_alpha, np.finfo(float).eps)/max(self.open_alpha, np.finfo(float).eps)):+.2f} dB"
        ble_notify_gap_last = 0.0
        ble_notify_gap_max = 0.0
        ble_notify_burst_max = 0
        ble_notify_gap_over_100ms = 0
        reliable = {
            "blocks_received": 0, "blocks_delivered": 0,
            "block_crc_bad": 0, "sync_drop": 0,
            "duplicates": 0, "out_of_order": 0,
            "retransmitted_received": 0, "gap_markers": 0,
            "ack_sent": 0, "nack_sent": 0,
            "control_errors": 0, "pending_blocks": 0,
            "max_pending": 0, "expected_block": 0,
            "watchdog_nacks": 0, "forced_skips": 0,
            "watchdog_reconnects": 0,
            "decode_queued_bytes": 0, "decode_peak_bytes": 0,
            "decode_errors": 0,
        }
        if self.ble_worker is not None:
            try:
                (ble_notify_gap_last, ble_notify_gap_max, ble_notify_burst_max,
                 ble_notify_gap_over_100ms) = self.ble_worker.timing_metrics()
                reliable.update(self.ble_worker.reliable_metrics())
            except Exception:
                pass
        serial_metrics = {
            "queued_bytes": 0, "peak_queued_bytes": 0, "read_calls": 0,
            "read_errors": 0, "overflow_events": 0, "last_gap_s": 0.0,
            "max_gap_s": 0.0, "buffer_configured": False, "buffer_error": "",
        }
        if self.serial_worker is not None:
            try:
                serial_metrics.update(self.serial_worker.metrics())
            except Exception:
                pass
        adaptive_ble = {
            "profile": "---", "samples": 0, "p95_s": 0.0, "ack_interval_s": 0.0,
            "nack_repeat_s": 0.0, "stall_reconnect_s": 0.0,
        }
        if self.ble_worker is not None:
            try:
                adaptive_ble.update(self.ble_worker.adaptive_timing())
            except Exception:
                pass
        raw_name = Path(raw_path).name if raw_path != "---" else "---"
        filter_metrics = {
            "queued_samples": 0, "output_samples": 0, "peak_queued_samples": 0,
            "batches_processed": 0, "errors": 0, "display_dropped_samples": 0,
            "last_error": "",
        }
        if self.filter_worker is not None:
            try:
                filter_metrics.update(self.filter_worker.metrics())
            except Exception:
                pass

        # V18 diagnostics: the 48-byte frame only carries the low 8 bits of the
        # firmware queue-drop counter.  A long BLE stall can therefore hide a
        # >255-frame device-side drop from the per-frame attribution logic.
        # The full 32-bit STATUS counter is authoritative for the session.
        fw_queue_drop_total = int(self.ble_status.get("queue_drop", 0)) if self.active_transport == "ble" else 0
        if self.active_transport == "ble":
            gap_device_display = max(
                int(self.seq_device_lost),
                min(int(self.seq_lost), fw_queue_drop_total),
            )
            gap_host_display = max(0, int(self.seq_lost) - gap_device_display)
        else:
            gap_device_display = int(self.seq_device_lost)
            gap_host_display = int(self.seq_host_lost)

        entries = [
            ("Transport", self.transport_description()),
            ("Mode", mode),
            ("Streaming", str(int(self.streaming))),
            ("Channel", selected_config),
            ("Estimated Fs", f"{fs_text} Hz"),
            ("Frames", str(self.packet_count)),
            ("Sequence gaps", f"{self.seq_lost} / {self.seq_gap_events} evt"),
            ("Gap MCU/host", f"{gap_device_display} / {gap_host_display}"),
            ("CRC / sync", f"{self.parser.crc_bad} / {self.parser.sync_drop}"),
            ("Pending / queue", f"{self.last_pending} / {self.last_queue_depth}"),
            ("Queue-drop hints", str(self.queue_drop_hints)),
            ("Backlog events", str(self.backlog_events)),
            ("RX now / peak", f"{self.last_serial_waiting_bytes} / {self.transport_peak_pending_bytes} B"),
            ("Serial worker", f"{serial_metrics['queued_bytes']}/{serial_metrics['peak_queued_bytes']} B"),
            ("Serial gap/err", f"{1000*serial_metrics['max_gap_s']:.0f} ms / {serial_metrics['read_errors']}"),
            ("Serial OS buffer", "1MB" if serial_metrics['buffer_configured'] else "driver default"),
            ("Turn last/max", f"{self.transport_last_turn_ms:.2f}/{self.transport_max_turn_ms:.2f} ms"),
            ("RX lag", f"{self.live_lag_s:.3f} s"),
            ("Render gap", f"{self.render_gap_last_ms:.1f}/{self.render_gap_max_ms:.1f} ms"),
            ("Notify gap", f"{1000*ble_notify_gap_last:.1f}/{1000*ble_notify_gap_max:.1f} ms"),
            ("BLE adapt", f"{adaptive_ble['profile']} p95={1000*adaptive_ble['p95_s']:.0f}ms"),
            ("BLE repair", f"N{1000*adaptive_ble['nack_repeat_s']:.0f}/S{adaptive_ble['stall_reconnect_s']:.1f}s"),
            ("Notify burst", f"{ble_notify_burst_max} B"),
            ("Display buffer", f"{self.display_delay_s:.3f} s"),
            ("Underrun / resync", f"{self.display_buffer_underruns} / {self.display_low_latency_resyncs}"),
            ("Paint / PSD skip", f"{self.ble_catchup_plot_skips} / {self.ble_psd_skips}"),
            ("BLE MTU", str(self.ble_status.get('mtu', self.ble_peer_mtu) if self.active_transport == 'ble' else '---')),
            ("FW frameQ/notifyErr", f"{self.ble_status.get('queue_drop', 0)} / {self.ble_status.get('notify_error', 0)}"),
            ("FW cmd/MTU", f"{self.ble_status.get('command_drop', 0)} / {self.ble_status.get('mtu_blocked', 0)}"),
            ("Reliable RX", f"{reliable['blocks_received']} / {reliable['blocks_delivered']}"),
            ("Reliable pending", f"{reliable['pending_blocks']} / max {reliable['max_pending']}"),
            ("BLE decode Q/peak", f"{reliable['decode_queued_bytes']} / {reliable['decode_peak_bytes']} B"),
            ("BLE decode errors", str(reliable['decode_errors'])),
            ("Retrans / gap", f"{reliable['retransmitted_received']} / {reliable['gap_markers']}"),
            ("ACK / NACK", f"{reliable['ack_sent']} / {reliable['nack_sent']}"),
            ("Watchdog N/S/R", f"{reliable['watchdog_nacks']}/{reliable['forced_skips']}/{reliable['watchdog_reconnects']}"),
            ("Dup / OOO", f"{reliable['duplicates']} / {reliable['out_of_order']}"),
            ("Reliable CRC/sync", f"{reliable['block_crc_bad']} / {reliable['sync_drop']}"),
            ("Control errors", str(reliable['control_errors'])),
            ("Stale ctrl suppressed", f"N{reliable['stale_nack_suppressed']} / A{reliable['stale_ack_suppressed']}"),
            ("FW retained/flight", f"{self.ble_status.get('reliable_stored', 0)} / {self.ble_status.get('reliable_outstanding', 0)}"),
            ("FW ACK / NACK", f"{self.ble_status.get('reliable_ack_count', 0)} / {self.ble_status.get('reliable_nack_count', 0)}"),
            ("FW retrans/recover", f"{self.ble_status.get('reliable_retransmit', 0)} / {self.ble_status.get('reliable_recovered', 0)}"),
            ("FW overflow/unknown", f"{self.ble_status.get('reliable_overflow', 0)} / {self.ble_status.get('reliable_unknown_nack', 0)}"),
            ("FW recent o/u/p", f"{self.ble_status_delta.get('reliable_overflow', 0)} / {self.ble_status_delta.get('reliable_unknown_nack', 0)} / {self.ble_status_delta.get('reliable_protocol_error', 0)}"),
            ("Filter queue/out", f"{filter_metrics['queued_samples']} / {filter_metrics['output_samples']} smp"),
            ("Filter peak/batch", f"{filter_metrics['peak_queued_samples']} / {filter_metrics['batches_processed']}"),
            ("Filter error/drop", f"{filter_metrics['errors']} / {filter_metrics['display_dropped_samples']}"),
            ("Raw queue", f"{self.raw_writer.queued_bytes} B"),
            ("Write / plot err", f"{self.raw_write_errors} / {self.plot_errors}"),
            ("Raw file", raw_name),
            ("Raw / filt RMS", f"{raw_rms} / {filtered_rms}"),
            ("Peak-to-peak", raw_pp),
            ("Valid samples", valid_ratio),
            ("50Hz ratio", line_ratio),
            ("Alpha peak/rel", f"{alpha_peak} / {alpha_rel}"),
            ("Saturation", f"{saturation:.4f}%"),
            ("Quality", quality),
            ("BIAS mask", f"0x{self.current_bias_mask():02X}"),
            ("Alpha C/O", comparison),
        ]

        def compact_item(item):
            label, value = item
            value = str(value).replace("\n", " ")
            if len(value) > 29:
                value = value[:28] + "…"
            return f"{label:<19}: {value:<29}"

        columns = 3
        rows = (len(entries) + columns - 1) // columns
        lines = []
        for row in range(rows):
            cells = []
            for col in range(columns):
                idx = row + col * rows
                cells.append(compact_item(entries[idx]) if idx < len(entries) else " " * 51)
            lines.append(" | ".join(cells).rstrip())
        lines.append("-" * 118)
        lines.append(f"判断: {verdict}")
        text = "\n".join(lines)

        bar = self.info_text.verticalScrollBar()
        previous_value = bar.value()
        was_at_bottom = previous_value >= max(0, bar.maximum() - 2)
        if text != self.info_text.toPlainText():
            self.info_text.setPlainText(text)
            bar = self.info_text.verticalScrollBar()
            if was_at_bottom:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(previous_value, bar.maximum()))

    def make_verdict(self, saturation: float) -> str:
        if self.packet_count < FS * 2 and self.offline_uv is None:
            return "数据不足，至少采集 2 秒。"
        if self.parser.crc_bad > 0:
            return "有 CRC 错：先查传输缓存、帧格式以及 USB/BLE 链路。"
        if self.status_bad > 0:
            return "ADS STATUS 异常：怀疑 SPI 位/字节错位。"

        reliable = self.ble_worker.reliable_metrics() if self.ble_worker is not None else {}
        if self.active_transport == "ble":
            fw_queue_drop_total = int(self.ble_status.get("queue_drop", 0))
            effective_device_gap = max(
                int(self.seq_device_lost),
                min(int(self.seq_lost), fw_queue_drop_total),
            )
            effective_host_gap = max(0, int(self.seq_lost) - effective_device_gap)

            overflow_total = int(self.ble_status.get("reliable_overflow", 0))
            unknown_total = int(self.ble_status.get("reliable_unknown_nack", 0))
            protocol_total = int(self.ble_status.get("reliable_protocol_error", 0))
            overflow_recent = int(self.ble_status_delta.get("reliable_overflow", 0))
            unknown_recent = int(self.ble_status_delta.get("reliable_unknown_nack", 0))
            protocol_recent = int(self.ble_status_delta.get("reliable_protocol_error", 0))

            if overflow_recent > 0:
                return (
                    "BLE 固件可靠保留环刚发生新增溢出；采集仍会继续，但这段可能有真实 EEG 缺口。"
                    "重点看 Notify gap、FW retained/flight 和 frameQueue drop，而不是 GUI 绘图。"
                )
            if int(reliable.get("forced_skips", 0)) > 0:
                return (
                    "BLE 曾有无法及时补回的 block；V18 会保留真实缺口并继续后续采集。"
                    "不会为了等待单个 block 把整条时域链长期堵住。"
                )
            if protocol_recent > 0:
                return (
                    "BLE 控制包刚出现格式/CRC/类型异常；V18 已把过期 ACK/NACK 从协议错误中分离并在发送前抑制。"
                    "若 recent p 持续增长，再检查 GUI/固件是否确为同一 V18 包。"
                )
            if unknown_recent > 0:
                return (
                    "BLE 刚有重传请求未命中保留块；V18 固件会忽略已 ACK 的过期 NACK，"
                    "只有真正仍未确认且已不在保留环的请求才计 unknown。继续看 overflow/gap 是否同步增长。"
                )
            # Historical counters are intentionally not a permanent error. In
            # V17 a stale NACK race could increment unknown once and make the
            # verdict stay red for the rest of an overnight capture.
            if (overflow_total or unknown_total or protocol_total) and (
                overflow_recent == 0 and unknown_recent == 0 and protocol_recent == 0
            ):
                pass
            if fw_queue_drop_total > 0:
                return (
                    f"MCU frameQueue 已实际丢 {fw_queue_drop_total} 帧；这不是 GUI/Serial 误报。"
                    "若同时看到 Notify gap 很大或 notifyErr 增长，说明 BLE 栈暂停曾拖住旧版传输任务；"
                    "V18 已将 frameQueue→可靠环 与 BLE notify 拆成独立任务。"
                )
            if (
                int(self.ble_status.get("notify_error", 0))
                or int(self.ble_status.get("command_drop", 0))
                or int(self.ble_status.get("mtu_blocked", 0))
            ):
                return (
                    "BLE Notify/控制/MTU 仍有异常计数，但 MCU frameQueue 尚未丢帧。"
                    "继续看 Notify gap、Reliable retained/flight 与 retrans/recover。"
                )
            if effective_device_gap > 0 or self.backlog_events > 0 or self.queue_drop_hints > 0:
                return "MCU/DRDY 或固件采集链确有缺口：优先检查 SPI 实时性和 MCU frameQueue。"
            if effective_host_gap > 0:
                return (
                    "BLE ADS 序号仍有主机侧缺口，但 MCU 未报告采集队列丢帧；"
                    "优先看 reliable pending/重传/decode queue。"
                )
        else:
            if self.seq_device_lost > 0 or self.backlog_events > 0 or self.queue_drop_hints > 0:
                return "MCU/DRDY 或固件队列确有缺口：优先检查 SPI 实时性、任务积压和固件 queue drop。"
            if self.seq_host_lost > 0:
                return "USB 序号有缺口但 MCU 未报告 pending/queue drop：检查 Serial worker 与 OS 串口缓存。"

        if np.isfinite(self.fs_est) and abs(self.fs_est - FS) > 2:
            return f"采样率 {self.fs_est:.1f} Hz 偏离 250 Hz。"
        if saturation > 0.1:
            return "有样本接近满量程：V18 会继续采集/保存，并只隔离该通道的实时绘图负担；仍需检查参考、BIAS 或电极。"
        if np.isfinite(self.latest_line_ratio) and self.latest_line_ratio > 0.25:
            return "原始 50 Hz 占比高：陷波后看着干净也不代表硬件健康。"
        if not self.latest_window_good:
            return f"当前 Alpha 窗被拒绝：{self.latest_window_reason}。"
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            delta = 10 * np.log10(max(self.closed_alpha, np.finfo(float).eps) / max(self.open_alpha, np.finfo(float).eps))
            return f"20秒中位数：闭眼/睁眼 Alpha = {delta:+.2f} dB。"
        return "数字链路与当前分析窗正常；可分别采集20秒睁眼和闭眼。"

    def clear_stats(self):
        self.ring.clear()
        self.reset_processing_state()
        self.parser.reset()
        self.reset_live_session_metrics()
        if self.ble_worker is not None:
            self.ble_worker.reset_timing_metrics()
        self.reset_display_jitter_buffer()
        self.max_read_us = 0
        self.latest_alpha_power = np.nan
        self.latest_alpha_peak = np.nan
        self.latest_alpha_rel = np.nan
        self.latest_raw_rms = np.nan
        self.latest_filtered_rms = np.nan
        self.latest_raw_pp = np.nan
        self.latest_line_ratio = np.nan
        self.latest_valid_ratio = np.nan
        self.latest_window_good = False
        self.latest_window_reason = "尚未分析"
        self.open_alpha = np.nan
        self.closed_alpha = np.nan
        self.alpha_capture_kind = None
        self.alpha_capture_values = []
        self.open_btn.setEnabled(True)
        self.closed_btn.setEnabled(True)
        self.set_status("统计已清空。")
        self.update_info_text()

    def closeEvent(self, event):  # noqa: N802
        APP_LOGGER.info("application close requested")
        try:
            self.stop_stream()
            if self.stream_server is not None:
                self.stream_server.stop()
                self.stream_server = None
            if self.serial_worker is not None:
                self.serial_worker.stop(timeout=2.0, close_port=False)
                self.serial_worker = None
            if self.active_transport == "serial" and self.ser and self.ser.is_open:
                self.ser.close()
            if self.ble_worker is not None:
                self.ble_worker.shutdown()
            if self.filter_worker is not None:
                self.filter_worker.shutdown()
        finally:
            APP_LOGGER.info("application resources closed")
            event.accept()


def main():
    global APP_LOGGER, APP_LOG_PATH
    APP_LOGGER, APP_LOG_PATH = configure_logging(LOG_DIR)
    APP_LOGGER.info("OmniBCI GUI firmware=V19 protocol=V1 source=%s frozen=%s", __file__, IS_FROZEN)

    original_excepthook = sys.excepthook

    def log_unhandled(exc_type, exc_value, exc_traceback):
        APP_LOGGER.critical(
            "unhandled main-thread exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        original_excepthook(exc_type, exc_value, exc_traceback)

    def log_thread_exception(args):
        APP_LOGGER.critical(
            "unhandled thread exception: %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def log_qt_message(message_type, context, message):
        level = {
            QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
            QtCore.QtMsgType.QtInfoMsg: logging.INFO,
            QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
            QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
            QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(message_type, logging.WARNING)
        location = f"{context.file}:{context.line}" if context and context.file else "Qt"
        APP_LOGGER.log(level, "%s %s", location, message)

    sys.excepthook = log_unhandled
    threading.excepthook = log_thread_exception
    QtCore.qInstallMessageHandler(log_qt_message)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("全域智能 ADS1299 EEG 工作站")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))
    win = MainWindow()
    win.show()
    watchdog = HangWatchdog(
        5.0,
        lambda elapsed: APP_LOGGER.critical(
            "GUI unresponsive for %.1f seconds; stacks=%s",
            elapsed,
            dump_all_thread_stacks(LOG_DIR, elapsed),
        ),
    )
    watchdog_stop = watchdog.start()
    heartbeat_timer = QtCore.QTimer(app)
    heartbeat_timer.setInterval(500)
    heartbeat_timer.timeout.connect(watchdog.heartbeat)
    heartbeat_timer.start()
    APP_LOGGER.info("GUI event loop starting; log=%s", APP_LOG_PATH)
    try:
        return app.exec()
    finally:
        watchdog_stop.set()
        APP_LOGGER.info("GUI event loop stopped")
        shutdown_logging(APP_LOGGER)


if __name__ == "__main__":
    sys.exit(main())
