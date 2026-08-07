"""Acquisition behavior for the main window."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
import serial
import serial.tools.list_ports
from PySide6 import QtCore, QtGui, QtWidgets
from scipy import signal

from ..constants import (
    BLE_MIN_STREAM_MTU,
    CHANNELS,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class AcquisitionMixin:
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
