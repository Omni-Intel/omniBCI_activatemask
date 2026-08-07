"""Impedance behavior for the main window."""

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
    CHANNELS,
    FS,
    LEAD_OFF_CURRENT_NA,
    LEAD_OFF_FREQUENCY_HZ,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class ImpedanceMixin:
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
            self.transport_write(bytes((0xA9, mask & 0xFF)))
            ack = self.read_config_ack(0xA9, expected_argument=mask & 0xFF)
            expected_p = 0 if self.reference_is_srb2() else mask
            expected_n = mask if self.reference_is_srb2() else 0
            if (
                ack is None
                or not ack["verified"]
                or ack["loff_p"] != expected_p
                or ack["loff_n"] != expected_n
                or ack["loff_config"] != 0x02
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
                self.transport_write(bytes((0xA9, 0x00)))
                ack = self.read_config_ack(0xA9, expected_argument=0x00)
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
