"""DeviceConfig behavior for the main window."""

from __future__ import annotations

import asyncio
import json
import os
import queue
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
    ADC_SATURATION_FRACTION,
    CHANNELS,
    LEAD_OFF_SERIES_SRB1_KOHM,
    LEAD_OFF_SERIES_SRB2_KOHM,
    MODE_ITEMS,
    REFERENCE_SRB1,
    REFERENCE_SRB2,
    VALID_GAINS,
    VREF,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class DeviceConfigMixin:
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
            enabled = bool(self.channel_enabled[ch])
            bias = "BIAS✓" if self.channel_bias[ch] else "BIAS—"
            power = "ON" if enabled else "OFF"
            if self.reference_is_srb2():
                reference = "SRB2✓" if self.channel_srb2[ch] and enabled else "SRB2—"
            else:
                reference = "SRB1全局"
            button.setText(f"CH{ch+1}  {power}  ×{int(self.channel_gains[ch])}\n{bias}  {reference}")
            icon = QtGui.QPixmap(11, 11)
            icon.fill(QtGui.QColor("#56bd31" if enabled else "#8b969e"))
            button.setIcon(QtGui.QIcon(icon))
            button.setToolTip(
                f"CH{ch+1}: {'启用' if enabled else '禁用'}, PGA ×{int(self.channel_gains[ch])}, "
                f"{'参与' if self.channel_bias[ch] else '不参与'} {self.bias_register_name()}；"
                + (
                    f"SRB2 {'接入' if self.channel_srb2[ch] and enabled else '断开'}"
                    if self.reference_is_srb2()
                    else "EEG 模式使用全局 SRB1"
                )
            )


    def open_channel_settings(self, ch):
        self._select_channel(ch)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"CH{ch+1} 通道设置")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
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
        srb2 = QtWidgets.QCheckBox("该通道接入 SRB2 公共参考")
        srb2.setChecked(bool(self.channel_srb2[ch]))
        srb2.setEnabled(self.reference_is_srb2())
        form.addRow("SRB2", srb2)
        if self.reference_is_srb2():
            note_text = (
                "SRB2 模式：测量电极接 INxN，公共参考接 SRB2；"
                "MISC1.SRB1 关闭，BIAS 默认从 N 侧取样。"
            )
        else:
            note_text = (
                "SRB1 模式：测量电极接 INxP，公共参考接 SRB1；"
                "SRB1 全局开启，逐通道 SRB2 不生效。"
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
            reference = (
                f"SRB2 {'ON' if srb2.isChecked() else 'OFF'}"
                if self.reference_is_srb2()
                else "SRB1 GLOBAL"
            )
            summary.setText(
                f"CH{ch+1}  |  {'ON' if enabled.isChecked() else 'OFF'}  |  "
                f"PGA ×{gain.currentText()}  |  BIAS {'YES' if bias.isChecked() else 'NO'}  |  "
                f"{reference}"
            )

        enabled.toggled.connect(update_summary)
        gain.currentTextChanged.connect(update_summary)
        bias.toggled.connect(update_summary)
        srb2.toggled.connect(update_summary)
        update_summary()
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.apply_channel_settings(
            ch, enabled.isChecked(), int(gain.currentText()), bias.isChecked(), srb2.isChecked()
        )


    def apply_channel_settings(self, ch, enabled, gain, bias, srb2=None):
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        if srb2 is None:
            srb2 = bool(self.channel_srb2[ch])
        effective_srb2 = bool(srb2 and enabled and self.reference_is_srb2())
        flags = (
            (0x01 if enabled else 0)
            | (0x02 if bias and enabled else 0)
            | (0x04 if effective_srb2 else 0)
        )
        was_streaming = bool(self.streaming)
        ack = None
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
                self.transport_write(bytes([0xA7, ch & 0x07, gain & 0xFF, flags]))
                ack = self.read_config_ack(0xA7, expected_argument=ch & 0x07)
                if ack is None:
                    raise RuntimeError(
                        "固件未返回 A7 寄存器读回。请烧录带配置 ACK 的最新固件。"
                    )
                if ack["argument"] != (ch & 0x07) or not ack["verified"]:
                    raise RuntimeError(
                        f"ADS1299 配置校验失败：CH{ch+1}, "
                        f"CHnSET=0x{ack['channel_register']:02X}, "
                        f"BIAS_P=0x{ack['bias_p']:02X}, BIAS_N=0x{ack['bias_n']:02X}"
                    )
            self.channel_enabled[ch] = bool(enabled)
            self.channel_gains[ch] = int(gain)
            self.channel_bias[ch] = bool(bias and enabled)
            self.channel_srb2[ch] = bool(srb2 and enabled)
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
                f"；ADS读回 CHnSET=0x{ack['channel_register']:02X}, "
                f"P=0x{ack['bias_p']:02X}, N=0x{ack['bias_n']:02X}"
                if ack is not None else "；仅更新离线显示参数"
            )
            self.set_status(
                f"已确认 CH{ch+1}: {'ON' if enabled else 'OFF'}, PGA×{gain}, "
                f"{self.bias_register_name()}={'YES' if bias and enabled else 'NO'}, "
                + (
                    f"SRB2={'ON' if effective_srb2 else 'OFF'}"
                    if self.reference_is_srb2()
                    else "SRB1=GLOBAL"
                )
                + readback
            )
        except Exception as exc:
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


    @staticmethod
    def ble_reference_hint_from_name(name: str):
        normalized = str(name or "").strip().upper()
        if "SRB2" in normalized:
            return REFERENCE_SRB2
        if "SRB1" in normalized:
            return REFERENCE_SRB1
        return None


    def _ble_write_channel_config(self, ch: int, reference_mode: int):
        """Legacy A7 path kept for manual per-channel changes."""
        enabled = bool(self.channel_enabled[ch])
        flags = (
            (0x01 if enabled else 0)
            | (0x02 if self.channel_bias[ch] and enabled else 0)
            | (
                0x04
                if reference_mode == REFERENCE_SRB2
                and self.channel_srb2[ch]
                and enabled
                else 0
            )
        )
        last_ack = None
        for attempt in range(1, 4):
            self.transport_reset_input_buffer(reset_reliable=False)
            self.transport_write(bytes((0xA7, ch, int(self.channel_gains[ch]), flags)))
            ack = self.read_config_ack(
                0xA7,
                timeout=1.8,
                expected_argument=ch,
            )
            last_ack = ack
            if ack is not None and ack["verified"]:
                return ack
            time.sleep(0.08 * attempt)

        detail = "无 ACK"
        if last_ack is not None:
            detail = (
                f"verified={int(bool(last_ack['verified']))}, "
                f"CHnSET=0x{last_ack['channel_register']:02X}, "
                f"P=0x{last_ack['bias_p']:02X}, N=0x{last_ack['bias_n']:02X}"
            )
        raise RuntimeError(
            f"BLE 初始化时 CH{ch+1} 配置连续 3 次读回失败（{detail}）"
        )


    def _ble_write_bulk_config(self, reference_mode: int):
        """Configure all eight channels in one ADS stop/write/verify transaction."""
        enabled_mask = sum(
            (1 << ch) for ch in range(CHANNELS) if self.channel_enabled[ch]
        ) & 0xFF
        bias_mask = sum(
            (1 << ch)
            for ch in range(CHANNELS)
            if self.channel_enabled[ch] and self.channel_bias[ch]
        ) & 0xFF
        srb2_mask = sum(
            (1 << ch)
            for ch in range(CHANNELS)
            if (
                reference_mode == REFERENCE_SRB2
                and self.channel_enabled[ch]
                and self.channel_srb2[ch]
            )
        ) & 0xFF
        gains = [int(v) & 0xFF for v in self.channel_gains]
        packet = bytes((
            0xA5,
            int(reference_mode) & 0x01,
            enabled_mask,
            bias_mask,
            srb2_mask,
            *gains,
        ))
        last_ack = None
        for attempt in range(1, 4):
            self.transport_reset_input_buffer(reset_reliable=False)
            self.transport_write(packet)
            ack = self.read_config_ack(
                0xA5,
                timeout=2.8,
                expected_argument=enabled_mask,
            )
            last_ack = ack
            if (
                ack is not None
                and ack["verified"]
                and int(ack.get("enabled_mask", -1)) == enabled_mask
            ):
                return ack
            time.sleep(0.10 * attempt)

        detail = "无 ACK"
        if last_ack is not None:
            detail = (
                f"verified={int(bool(last_ack['verified']))}, "
                f"enabled=0x{last_ack['enabled_mask']:02X}, "
                f"P=0x{last_ack['bias_p']:02X}, N=0x{last_ack['bias_n']:02X}, "
                f"reference={'SRB2' if last_ack.get('reference') else 'SRB1'}"
            )
        raise RuntimeError(
            "BLE 批量通道初始化连续 3 次失败（"
            + detail
            + "）。请烧录 V16 配套固件。"
        )


    def sync_ble_configuration(self, requested_reference=None, probe_capability: bool = True):
        """Apply one atomic A5 configuration instead of eight full A7 rewrites."""
        self.transport_write(b"s")
        time.sleep(0.08)

        if requested_reference is None:
            hinted = self.ble_reference_hint_from_name(self.ble_device_name)
            requested_reference = self.reference_mode if hinted is None else hinted
        requested_reference = (
            REFERENCE_SRB2
            if int(requested_reference) == REFERENCE_SRB2
            else REFERENCE_SRB1
        )

        supports_srb2 = bool(self.ble_supports_srb2)
        if probe_capability:
            self.transport_reset_input_buffer(reset_reliable=False)
            self.transport_write(bytes((0xA8, REFERENCE_SRB2)))
            time.sleep(0.06)
            self.transport_write(b"p")
            time.sleep(0.06)
            probe_ack = self._ble_write_bulk_config(REFERENCE_SRB2)
            supports_srb2 = (
                int(probe_ack.get("reference", REFERENCE_SRB1)) == REFERENCE_SRB2
            )

        actual_reference = (
            REFERENCE_SRB2
            if requested_reference == REFERENCE_SRB2 and supports_srb2
            else REFERENCE_SRB1
        )
        self.transport_reset_input_buffer(reset_reliable=False)
        self.transport_write(bytes((0xA8, actual_reference)))
        time.sleep(0.06)
        self.transport_write(b"p")
        time.sleep(0.06)
        ack = self._ble_write_bulk_config(actual_reference)
        if int(ack.get("reference", REFERENCE_SRB1)) != actual_reference:
            raise RuntimeError(
                f"BLE 参考模式读回不一致：请求 "
                f"{('SRB2' if actual_reference else 'SRB1')}，固件返回 "
                f"{('SRB2' if ack.get('reference') else 'SRB1')}"
            )
        self.transport_reset_input_buffer(clear_status=True, reset_reliable=False)
        return actual_reference, supports_srb2


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
