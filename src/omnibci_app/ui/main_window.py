
"""Composition root for the OmniBCI main window."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import numpy as np
import pyqtgraph as pg
import serial
from PySide6 import QtCore, QtGui, QtWidgets
from scipy import signal

from ..constants import (
    CHANNELS,
    DISPLAY_JITTER_BASE_TARGET_S,
    DISPLAY_JITTER_MIN_RESERVE_S,
    DISPLAY_JITTER_STARTUP_S,
    FILTER_RESULT_POLL_MS,
    FS,
    PSD_LIVE_REFRESH_MS,
    REFERENCE_SRB2,
    SERIAL_PLOT_INTERVAL_MS,
    SERIAL_POLL_INTERVAL_MS,
)
from ..processing import ClockAxisItem, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser
from ..recording import AsyncRawWriter
from ..transports import BLE_AVAILABLE, BleTransportWorker, SerialTransportWorker
from .acquisition import AcquisitionMixin
from .device_config import DeviceConfigMixin
from .file_io import FileIOMixin
from .impedance import ImpedanceMixin
from .layout import LayoutMixin
from .signal_processing import SignalProcessingMixin
from .transport_control import TransportControlMixin
from .visualization import VisualizationMixin


class MainWindow(
    LayoutMixin,
    DeviceConfigMixin,
    FileIOMixin,
    TransportControlMixin,
    SignalProcessingMixin,
    ImpedanceMixin,
    AcquisitionMixin,
    VisualizationMixin,
    QtWidgets.QMainWindow,
):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("全域智能 | ADS1299 EEG 工作站 | V18 | BLE continuity + stale-NACK hardening")
        self.resize(1500, 920)

        self.gain = 24  # legacy/global command value
        self.channel_gains = np.full(CHANNELS, 24, dtype=np.int16)
        self.channel_enabled = np.array([True] * 5 + [False] * 3, dtype=bool)
        self.channel_bias = np.array([True] * 5 + [False] * 3, dtype=bool)
        # Default: measurement electrodes on INxN and the common reference
        # electrode on SRB2. SRB1 remains selectable for boards that route it.
        self.reference_mode = REFERENCE_SRB2
        self.channel_srb2 = np.array([True] * 5 + [False] * 3, dtype=bool)
        self.lsb_uv = self.calc_lsb_uv()
        self.ring = RingBuffer(CHANNELS, FS * 90)            # untouched input-referred uV
        self.filtered_ring = RingBuffer(CHANNELS, FS * 90)   # continuous causal display chain
        self.parser = AdsFrameParser(self.channel_lsb_uv)
        self.filter_generation = 0
        self.filter_worker: Optional[LiveFilterWorker] = None
        self.filter_batches_applied = 0
        self.filter_stale_batches = 0
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
