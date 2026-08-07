
"""ADS1299 frame and reliable BLE payload decoding."""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .constants import (
    BLE_COMPACT_FRAME_BYTES,
    CHANNELS,
    FRAME_BYTES,
    LIVE_TIMELINE_MAX_FILL_SAMPLES,
    SYNC1,
    SYNC2,
)

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
