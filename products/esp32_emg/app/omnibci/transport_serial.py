"""Dedicated serial reader thread that drains the Windows driver buffer."""

from __future__ import annotations

import threading
import time
from collections import deque

from .constants import (
    SERIAL_HOST_MAX_QUEUE_BYTES,
    SERIAL_MAX_PROCESS_BYTES,
    SERIAL_READER_MAX_READ_BYTES,
)

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


