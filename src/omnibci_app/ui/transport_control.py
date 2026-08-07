"""TransportControl behavior for the main window."""

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
    BAUD,
    BLE_COALESCE_MAX_HOLD_S,
    BLE_COALESCE_MIN_BYTES,
    BLE_MAX_PROCESS_BYTES,
    BLE_MIN_STREAM_MTU,
    BLE_PLOT_INTERVAL_MS,
    BLE_POLL_INTERVAL_MS,
    BYTES_PER_SECOND,
    CHANNELS,
    FS,
    REFERENCE_SRB2,
    SERIAL_MAX_PROCESS_BYTES,
    SERIAL_PLOT_INTERVAL_MS,
    SERIAL_POLL_INTERVAL_MS,
    SERIAL_READER_TIMEOUT_S,
    SERIAL_REPOLL_DELAY_MS,
    SERIAL_RX_BUFFER_BYTES,
    TRANSPORT_REPOLL_DELAY_MS,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class TransportControlMixin:
    def set_status(self, text: str):
        self.status_label.setText(text)


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
                "连接后自动识别固定 SRB1 固件或可切换 SRB1/SRB2 固件。"
            )
        else:
            self.serial_label.setText("串口")
            self.refresh_btn.setText("扫描串口")
            self.connect_btn.setText("打开串口")
            self.reference_combo.setEnabled(True)
            self.apply_reference_btn.setEnabled(True)
            self.reference_combo.setToolTip(
                "SRB1：每通道信号接 INxP，参考接 SRB1；"
                "SRB2：每通道信号接 INxN，参考接 SRB2。"
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
            self.reference_combo.setEnabled(bool(self.ble_supports_srb2))
            self.apply_reference_btn.setEnabled(bool(self.ble_supports_srb2))
            self.set_status(
                f"BLE 已自动重连：{name}，MTU={mtu}；正在继续原可靠会话并补传断线期间数据。"
            )
            return

        hinted_reference = self.ble_reference_hint_from_name(name)
        requested_reference = (
            self.reference_mode
            if reconnected or hinted_reference is None
            else hinted_reference
        )
        try:
            self.transport_reset_input_buffer()
            actual_reference, supports_srb2 = self.sync_ble_configuration(
                requested_reference=requested_reference,
                probe_capability=not reconnected or self.ble_reference_profile == "unknown",
            )
            self.ble_supports_srb2 = bool(supports_srb2)
            self.ble_reference_profile = "dual" if supports_srb2 else "srb1_fixed"
            self.set_reference_mode_local(actual_reference)
            if actual_reference == REFERENCE_SRB2:
                for ch in range(CHANNELS):
                    if self.channel_enabled[ch]:
                        self.channel_srb2[ch] = True
            self.reference_combo.setEnabled(bool(supports_srb2))
            self.apply_reference_btn.setEnabled(bool(supports_srb2))
            self.reference_combo.setToolTip(
                "此 BLE 固件支持运行时切换 SRB1/SRB2。"
                if supports_srb2
                else "此 BLE 固件固定为 SRB1。"
            )
            self.refresh_channel_parameter_labels()
            self.current_mode = 1
            self.mode_before_internal_short = 1
            self.mode_combo.setCurrentIndex(self._mode_index_from_code(1))
            self._sync_internal_short_button(False)
            action = "已自动重连" if reconnected else "已连接"
            capability = "可切换 SRB1/SRB2" if supports_srb2 else "固定 SRB1"
            self.set_status(
                f"BLE {action}：{name}，MTU={mtu}，当前 {self.reference_short_name()}，"
                f"{capability}。"
                + ("采集已恢复。" if reconnected and self.streaming else "点击“开始采集”。")
            )
        except Exception as exc:
            self.set_status(f"BLE 已连接，但参考模式/通道初始化失败：{exc}")


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
        if (len(data) < 72 or data[2] not in (0x03, 0x04)) and not self.ble_protocol_warned:
            self.ble_protocol_warned = True
            self.set_status(
                "BLE 固件协议不匹配：请烧录可靠传输协议 V3/V4 的 SRB1 或 SRB2 固件。"
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
