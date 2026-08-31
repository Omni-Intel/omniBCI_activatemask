"""In-memory EEG timeline ring buffers (raw and filtered chains)."""

from __future__ import annotations

from typing import Tuple

import numpy as np

from .protocol import Frame

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


