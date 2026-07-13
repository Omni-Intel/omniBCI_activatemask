# -*- coding: utf-8 -*-
"""
ADS1299 EEG Native Python GUI
--------------------------------
Fast PyQtGraph-based replacement for the MATLAB diagnostic GUI.

Protocol expected from firmware:
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
where XX is bit mask for CH1..CH8. This only changes ADS1299 register 0x0D.
Your firmware must support this binary command.
"""

from __future__ import annotations

import os
import sys
import time
import struct
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Tuple

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
    from PySide6 import QtCore, QtWidgets
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing PySide6. Run: pip install PySide6") from exc

try:
    import pyqtgraph as pg
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing pyqtgraph. Run: pip install pyqtgraph") from exc


FS = 250
CHANNELS = 8
BAUD = 921600
FRAME_BYTES = 48
SYNC1 = 0xA5
SYNC2 = 0x5A
VREF = 4.5
VALID_GAINS = [1, 2, 4, 6, 8, 12, 24]
MODE_ITEMS = [
    ("EEG SRB1 + BIAS P-only", b"p", 1),
    ("EEG SRB1 + BIAS P+N", b"n", 0),
    ("EEG BIAS off", b"o", 2),
    ("ADS internal short", b"q", 3),
    ("ADS internal test square", b"t", 4),
]
MODE_NAMES = {
    0: "EEG/P+N",
    1: "EEG/P-only",
    2: "EEG/BIAS-off",
    3: "SHORTED",
    4: "TEST",
}


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
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


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

    def clear(self):
        self.data.fill(np.nan)
        self.valid.fill(False)
        self.seq.fill(0)
        self.mode.fill(0)
        self.head = 0
        self.count = 0

    def append(self, frame: Frame):
        self.data[:, self.head] = frame.uv
        self.valid[self.head] = frame.valid
        self.seq[self.head] = frame.sequence
        self.mode[self.head] = frame.mode
        self.head = (self.head + 1) % self.capacity
        self.count = min(self.count + 1, self.capacity)

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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADS1299 Native Python EEG GUI - fast PyQtGraph")
        self.resize(1500, 920)

        self.gain = 24
        self.lsb_uv = self.calc_lsb_uv()
        self.ring = RingBuffer(CHANNELS, FS * 90)
        self.parser = AdsFrameParser(lambda: self.lsb_uv)
        self.ser: Optional[serial.Serial] = None
        self.streaming = False
        self.raw_file = None
        self.raw_path = ""
        self.raw_bytes = 0
        self.offline_uv: Optional[np.ndarray] = None
        self.offline_valid: Optional[np.ndarray] = None
        self.offline_seq: Optional[np.ndarray] = None
        self.offline_mode: Optional[np.ndarray] = None
        self.offline_end = 0

        self.packet_count = 0
        self.status_bad = 0
        self.drdy_bad = 0
        self.seq_lost = 0
        self.backlog_events = 0
        self.queue_drop_hints = 0
        self.saturation_samples = 0
        self.last_seq: Optional[int] = None
        self.last_queue_drop_low = 0
        self.first_seq: Optional[int] = None
        self.first_clock: Optional[float] = None
        self.fs_est = np.nan
        self.current_mode = 1
        self.last_read_us = 0
        self.max_read_us = 0
        self.last_pending = 0
        self.last_queue_depth = 0
        self.latest_alpha_power = np.nan
        self.latest_alpha_peak = np.nan
        self.latest_alpha_rel = np.nan
        self.latest_rms = np.nan
        self.latest_line_ratio = np.nan
        self.open_alpha = np.nan
        self.closed_alpha = np.nan

        self.sos_band = signal.butter(2, [5, 50], btype="bandpass", fs=FS, output="sos")
        self.sos_notch = signal.butter(2, [48, 52], btype="bandstop", fs=FS, output="sos")

        self._build_ui()
        self.refresh_ports()

        self.serial_timer = QtCore.QTimer(self)
        self.serial_timer.timeout.connect(self.poll_serial)
        self.serial_timer.start(10)

        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.timeout.connect(self.update_fast_plots)
        self.plot_timer.start(80)

        self.psd_timer = QtCore.QTimer(self)
        self.psd_timer.timeout.connect(self.update_psd_and_info)
        self.psd_timer.start(1000)

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
        self.psd_max_spin.setValue(40)
        controls.addWidget(self.psd_max_spin, row, 7)

        self.psd_raw_check = QtWidgets.QCheckBox("PSD用宽频原始")
        controls.addWidget(self.psd_raw_check, row, 8)

        self.open_btn = QtWidgets.QPushButton("保存睁眼窗")
        self.open_btn.clicked.connect(lambda: self.store_alpha(False))
        controls.addWidget(self.open_btn, row, 9)
        self.closed_btn = QtWidgets.QPushButton("保存闭眼窗")
        self.closed_btn.clicked.connect(lambda: self.store_alpha(True))
        controls.addWidget(self.closed_btn, row, 10)
        self.clear_btn = QtWidgets.QPushButton("清空统计")
        self.clear_btn.clicked.connect(self.clear_stats)
        controls.addWidget(self.clear_btn, row, 11)

        row += 1
        controls.addWidget(QtWidgets.QLabel("bin名"), row, 0)
        self.bin_name = QtWidgets.QLineEdit("eeg_%TIME%_gain%GAIN%.bin")
        controls.addWidget(self.bin_name, row, 1, 1, 4)
        self.import_btn = QtWidgets.QPushButton("导入bin")
        self.import_btn.clicked.connect(self.import_bin)
        controls.addWidget(self.import_btn, row, 5)
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offline_slider.setEnabled(False)
        self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        controls.addWidget(self.offline_slider, row, 6, 1, 4)
        self.offline_label = QtWidgets.QLabel("未导入")
        controls.addWidget(self.offline_label, row, 10, 1, 2)

        row += 1
        bias_box = QtWidgets.QGroupBox("BIAS_SENSP：勾选参与 BIAS 正端平均的通道，只写 0x0D，BIAS_SENSN 不动")
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
        self.bias_apply_btn = QtWidgets.QPushButton("应用 BIAS_SENSP")
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
        self.status_label = QtWidgets.QLabel("未连接。这个版本用 PyQtGraph 原生刷新，不用 MATLAB 式整窗重算。")
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
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen(width=1.5))
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

    # ---------------- helpers ----------------
    def calc_lsb_uv(self) -> float:
        return VREF / (self.gain * (2**23 - 1)) * 1e6

    def set_status(self, text: str):
        self.status_label.setText(text)

    def refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            self.port_combo.addItem("无可用串口")
        else:
            self.port_combo.addItems(ports)
            if current in ports:
                self.port_combo.setCurrentText(current)

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.disconnect_serial()
            return
        port = self.port_combo.currentText()
        if not port or port == "无可用串口":
            QtWidgets.QMessageBox.warning(self, "串口", "没有可用串口。")
            return
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0, write_timeout=0)
            # USB CDC may reset the ESP32. Avoid long blocking; just wait a little and flush.
            QtWidgets.QApplication.processEvents()
            time.sleep(0.7)
            self.ser.reset_input_buffer()
            self.ser.write(b"s")
            self.connect_btn.setText("断开")
            self.set_status(f"已连接 {port}。点击开始/保存bin。")
        except Exception as exc:
            self.ser = None
            QtWidgets.QMessageBox.critical(self, "连接失败", str(exc))

    def disconnect_serial(self):
        self.stop_stream()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connect_btn.setText("连接")
        self.set_status("未连接。")

    def require_serial(self) -> bool:
        if not self.ser or not self.ser.is_open:
            QtWidgets.QMessageBox.warning(self, "串口", "请先连接串口。")
            return False
        return True

    def make_raw_path(self) -> str:
        name = self.bin_name.text().strip() or "eeg_%TIME%_gain%GAIN%.bin"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = name.replace("%TIME%", stamp).replace("%GAIN%", str(self.gain))
        path = Path(name)
        if not path.is_absolute():
            folder = Path.cwd() / "recordings"
            folder.mkdir(exist_ok=True)
            path = folder / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    # ---------------- serial/actions ----------------
    def start_stream(self):
        if not self.require_serial():
            return
        try:
            self.offline_uv = None
            self.offline_slider.setEnabled(False)
            self.offline_label.setText("实时")
            self.parser.reset()
            self.ring.clear()
            self.raw_path = self.make_raw_path()
            self.raw_file = open(self.raw_path, "wb")
            self.raw_bytes = 0
            self.ser.reset_input_buffer()
            self.ser.write(b"b")
            self.streaming = True
            self.set_status(f"实时采集中，raw bin 保存到：{self.raw_path}")
        except Exception as exc:
            self.close_raw_file()
            QtWidgets.QMessageBox.critical(self, "开始失败", str(exc))

    def stop_stream(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b"s")
            except Exception:
                pass
        self.streaming = False
        self.close_raw_file()

    def close_raw_file(self):
        if self.raw_file:
            try:
                self.raw_file.flush()
                self.raw_file.close()
            except Exception:
                pass
        self.raw_file = None

    def apply_mode(self):
        if not self.require_serial():
            return
        idx = self.mode_combo.currentIndex()
        name, cmd, expected = MODE_ITEMS[idx]
        was_streaming = self.streaming
        try:
            self.ser.write(b"s")
            self.streaming = False
            time.sleep(0.08)
            self.ser.reset_input_buffer()
            self.parser.reset()
            self.ser.write(cmd)
            if cmd in (b"q", b"t"):
                self.filter_check.setChecked(False)
                self.psd_raw_check.setChecked(True)
            self.ring.clear()
            self.last_seq = None
            self.first_seq = None
            self.first_clock = None
            self.fs_est = np.nan
            self.current_mode = expected
            time.sleep(0.35)
            self.ser.reset_input_buffer()
            if was_streaming:
                self.ser.write(b"b")
                self.streaming = True
            self.set_status(f"模式已切换：{name}")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "模式切换失败", str(exc))

    def change_pga(self, text: str):
        try:
            new_gain = int(text)
        except ValueError:
            return
        if new_gain not in VALID_GAINS:
            return
        self.gain = new_gain
        self.lsb_uv = self.calc_lsb_uv()
        if self.ser and self.ser.is_open and self.offline_uv is None:
            try:
                self.ser.write(str(new_gain).encode("ascii"))
                self.ring.clear()
                self.last_seq = None
                self.set_status(f"已发送 PGA={new_gain}；显示 LSB 同步为 {self.lsb_uv:.6g} uV/code。")
            except Exception as exc:
                self.set_status(f"PGA 指令发送失败：{exc}")
        else:
            self.set_status(f"仅修改本地解码 PGA={new_gain}，LSB={self.lsb_uv:.6g} uV/code。")

    def poll_serial(self):
        if not self.ser or not self.ser.is_open:
            return
        try:
            n = self.ser.in_waiting
            if n <= 0:
                return
            data = self.ser.read(n)
            if self.raw_file and data:
                self.raw_file.write(data)
                self.raw_bytes += len(data)
            frames = self.parser.feed(data)
            if frames:
                self.process_frames(frames, live=True)
        except Exception as exc:
            self.set_status(f"串口读取异常：{exc}")

    def process_frames(self, frames: List[Frame], live: bool):
        now = time.perf_counter()
        for fr in frames:
            self.packet_count += 1
            if not (fr.flags & 0x01):
                self.status_bad += 1
            if not (fr.flags & 0x02):
                self.drdy_bad += 1
            if (fr.flags & 0x04) or fr.pending > 1:
                self.backlog_events += 1
            if self.last_seq is not None:
                delta = (fr.sequence - self.last_seq) & 0xFFFFFFFF
                if 1 < delta < 1_000_000:
                    self.seq_lost += delta - 1
            else:
                self.first_seq = fr.sequence
                self.first_clock = now
            self.last_seq = fr.sequence
            if live and self.first_clock is not None and self.first_seq is not None:
                elapsed = now - self.first_clock
                if elapsed > 1:
                    progressed = ((fr.sequence - self.first_seq) & 0xFFFFFFFF) + 1
                    self.fs_est = progressed / elapsed
            drop_delta = (fr.queue_drop_low - self.last_queue_drop_low) & 0xFF
            if self.packet_count > 1 and 0 < drop_delta < 128:
                self.queue_drop_hints += drop_delta
            self.last_queue_drop_low = fr.queue_drop_low
            self.saturation_samples += int(np.sum(np.abs(fr.raw_counts[:5]) > 0.95 * (2**23 - 1)))
            self.current_mode = fr.mode
            self.last_read_us = fr.read_us
            self.max_read_us = max(self.max_read_us, fr.read_us)
            self.last_pending = fr.pending
            self.last_queue_depth = fr.queue_depth
            if live:
                self.ring.append(fr)

    # ---------------- BIAS_SENSP ----------------
    def current_bias_mask(self) -> int:
        mask = 0
        for i, cb in enumerate(self.bias_checks):
            if cb.isChecked():
                mask |= (1 << i)
        return mask & 0xFF

    def update_bias_mask_label(self):
        self.bias_mask_label.setText(f"mask=0x{self.current_bias_mask():02X}")

    def set_bias_checks(self, mask: int):
        for i, cb in enumerate(self.bias_checks):
            cb.blockSignals(True)
            cb.setChecked(bool(mask & (1 << i)))
            cb.blockSignals(False)
        self.update_bias_mask_label()

    def apply_bias_sensp(self):
        mask = self.current_bias_mask()
        if not self.require_serial():
            return
        try:
            self.ser.write(bytes([0xA6, 0x0D, mask]))
            self.set_status(f"已发送 BIAS_SENSP = 0x{mask:02X}。注意：这个命令不改 BIAS_SENSN。")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "BIAS_SENSP 发送失败", str(exc))

    # ---------------- bin import/offline ----------------
    def import_bin(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "导入 raw bin", "", "BIN files (*.bin);;All files (*.*)")
        if not path:
            return
        try:
            self.stop_stream()
            raw = Path(path).read_bytes()
            parser = AdsFrameParser(lambda: self.lsb_uv)
            frames = parser.feed(raw)
            if not frames:
                QtWidgets.QMessageBox.warning(self, "导入失败", "没有解析出有效 48-byte 帧。")
                return
            self.offline_uv = np.stack([f.uv for f in frames], axis=1).astype(np.float32)
            self.offline_valid = np.array([f.valid for f in frames], dtype=bool)
            self.offline_seq = np.array([f.sequence for f in frames], dtype=np.uint32)
            self.offline_mode = np.array([f.mode for f in frames], dtype=np.uint8)
            self.offline_end = self.offline_uv.shape[1]
            self.offline_slider.setEnabled(True)
            self.offline_slider.setRange(1, self.offline_end)
            self.offline_slider.setValue(self.offline_end)
            self.offline_label.setText(f"{Path(path).name}: {self.offline_end/FS:.1f}s")
            self.set_status(f"已导入 {path}，有效帧 {self.offline_end}，CRC坏帧 {parser.crc_bad}。")
            self.update_fast_plots()
            self.update_psd_and_info()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "导入失败", str(exc))

    def offline_slider_changed(self, value: int):
        self.offline_end = int(value)
        if self.offline_uv is not None:
            self.offline_label.setText(f"{self.offline_end/FS:.1f}/{self.offline_uv.shape[1]/FS:.1f}s")
        self.update_fast_plots()

    def get_view_data(self, seconds: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = max(1, int(seconds * FS))
        if self.offline_uv is not None:
            end = max(1, min(self.offline_end, self.offline_uv.shape[1]))
            start = max(0, end - n)
            return self.offline_uv[:, start:end].copy(), self.offline_valid[start:end].copy(), self.offline_mode[start:end].copy()
        data, valid, _, mode = self.ring.latest(n)
        return data, valid, mode

    # ---------------- signal processing/plotting ----------------
    def clean_signal(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float).copy()
        good = np.isfinite(x)
        if good.all():
            return x
        if good.sum() < 2:
            return np.zeros_like(x)
        idx = np.arange(x.size)
        x[~good] = np.interp(idx[~good], idx[good], x[good])
        return x

    def filter_for_display(self, x: np.ndarray) -> np.ndarray:
        x = self.clean_signal(x)
        x = x - np.nanmean(x)
        if x.size < 64 or not self.filter_check.isChecked():
            return x
        # Native Python real-time display: causal/fast IIR, not MATLAB-style filtfilt every refresh.
        y = signal.sosfilt(self.sos_band, x)
        y = signal.sosfilt(self.sos_notch, y)
        return y

    def update_fast_plots(self):
        seconds = float(self.win_spin.value())
        data, valid, _mode = self.get_view_data(seconds)
        if data.shape[1] < 2:
            return
        ch = self.channel_combo.currentIndex()
        x = data[ch]
        if valid.size == x.size:
            x = x.copy()
            x[~valid] = np.nan
        y = self.filter_for_display(x)
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
        self.time_plot.setTitle(f"{self.channel_combo.currentText()} 时域 | {MODE_NAMES.get(self.current_mode, 'UNKNOWN')}")

        # Stack plot uses at most 5 s to remain snappy.
        stack_data, stack_valid, _ = self.get_view_data(min(5.0, seconds))
        if stack_data.shape[1] < 2:
            return
        stack_t = (np.arange(stack_data.shape[1]) - stack_data.shape[1] + 1) / FS
        filtered = []
        for c in range(CHANNELS):
            xx = stack_data[c].copy()
            if stack_valid.size == xx.size:
                xx[~stack_valid] = np.nan
            yy = self.filter_for_display(xx)
            filtered.append(yy)
        arr = np.vstack(filtered)
        std = np.nanmedian(np.nanstd(arr, axis=1))
        spacing = float(max(50.0, 5.0 * std if np.isfinite(std) else 100.0))
        for c, curve in enumerate(self.stack_curves):
            curve.setData(stack_t, arr[c] + (CHANNELS - 1 - c) * spacing)
        self.stack_plot.setXRange(float(stack_t[0]), float(stack_t[-1]), padding=0)
        self.stack_plot.setYRange(-spacing, CHANNELS * spacing, padding=0.02)
        ticks = [((CHANNELS - 1 - c) * spacing, f"CH{c+1}") for c in range(CHANNELS)]
        self.stack_plot.getAxis("left").setTicks([ticks])

    def update_psd_and_info(self):
        data, valid, _ = self.get_view_data(10.0)
        if data.shape[1] >= FS * 4:
            ch = self.channel_combo.currentIndex()
            x = data[ch].copy()
            if valid.size == x.size:
                x[~valid] = np.nan
            x = self.clean_signal(x)
            x = x - np.mean(x)
            if not self.psd_raw_check.isChecked():
                x_psd = self.filter_for_display(x)
            else:
                x_psd = x
            nperseg = min(FS * 4, (x_psd.size // 2) * 2)
            if nperseg >= FS * 2:
                f, p = signal.welch(x_psd, fs=FS, window="hann", nperseg=nperseg, noverlap=nperseg // 2, nfft=max(2048, 2 ** int(np.ceil(np.log2(nperseg)))))
                max_hz = float(self.psd_max_spin.value())
                mask = (f >= 1) & (f <= max_hz)
                self.psd_curve.setData(f[mask], 10.0 * np.log10(p[mask] + np.finfo(float).eps))
                self.psd_plot.setXRange(1, max_hz, padding=0)

                # Alpha metrics always from filtered EEG-like signal.
                x_alpha = self.filter_for_display(x)
                f2, p2 = signal.welch(x_alpha, fs=FS, window="hann", nperseg=nperseg, noverlap=nperseg // 2, nfft=max(2048, 2 ** int(np.ceil(np.log2(nperseg)))))
                alpha = (f2 >= 8) & (f2 <= 13)
                broad = (f2 >= 4) & (f2 <= 30)
                if np.any(alpha):
                    self.latest_alpha_power = float(np.trapz(p2[alpha], f2[alpha]))
                    af = f2[alpha]
                    self.latest_alpha_peak = float(af[int(np.argmax(p2[alpha]))])
                bp = float(np.trapz(p2[broad], f2[broad])) if np.any(broad) else np.nan
                self.latest_alpha_rel = self.latest_alpha_power / max(bp, np.finfo(float).eps) if np.isfinite(bp) else np.nan
                self.latest_rms = float(np.sqrt(np.mean(x_alpha**2)))
                line = (f >= 48) & (f <= 52)
                useful = (f >= 5) & (f <= 45)
                lp = float(np.trapz(p[line], f[line])) if np.any(line) else np.nan
                up = float(np.trapz(p[useful], f[useful])) if np.any(useful) else np.nan
                self.latest_line_ratio = lp / max(up, np.finfo(float).eps) if np.isfinite(lp) and np.isfinite(up) else np.nan
                self.psd_plot.setTitle(f"PSD | Alpha peak {self.latest_alpha_peak:.2f} Hz | Alpha {100*self.latest_alpha_rel:.1f}%")
        self.update_info_text()

    def store_alpha(self, closed: bool):
        if not np.isfinite(self.latest_alpha_power):
            QtWidgets.QMessageBox.warning(self, "Alpha", "先稳定采集/回放至少 8-10 秒。")
            return
        if closed:
            self.closed_alpha = self.latest_alpha_power
        else:
            self.open_alpha = self.latest_alpha_power
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            delta = 10 * np.log10(max(self.closed_alpha, np.finfo(float).eps) / max(self.open_alpha, np.finfo(float).eps))
            self.set_status(f"Alpha 闭眼/睁眼 = {delta:+.2f} dB")
        else:
            self.set_status("Alpha 窗已保存，继续保存另一种状态。")

    def update_info_text(self):
        fs_text = f"{self.fs_est:.2f}" if np.isfinite(self.fs_est) else "---"
        alpha_peak = f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "---"
        alpha_rel = f"{100*self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
        rms = f"{self.latest_rms:.2f} uV" if np.isfinite(self.latest_rms) else "---"
        line_ratio = f"{self.latest_line_ratio:.3f}" if np.isfinite(self.latest_line_ratio) else "---"
        saturation = 100 * self.saturation_samples / max(1, self.packet_count * 5)
        mode = MODE_NAMES.get(self.current_mode, "UNKNOWN")
        verdict = self.make_verdict(saturation)
        raw_path = self.raw_path if self.raw_path else "---"
        offline = "yes" if self.offline_uv is not None else "no"
        self.info_text.setPlainText(
            "\n".join([
                f"Mode             : {mode}",
                f"PGA / LSB         : {self.gain}x / {self.lsb_uv:.6g} uV/code",
                f"Streaming        : {int(self.streaming)}",
                f"Offline bin       : {offline}",
                f"Raw bin           : {raw_path}",
                f"Raw bytes         : {self.raw_bytes}",
                f"Frames parsed     : {self.packet_count}",
                f"Estimated Fs      : {fs_text} Hz",
                f"CRC bad           : {self.parser.crc_bad}",
                f"Sync drop bytes   : {self.parser.sync_drop}",
                f"ADS STATUS bad    : {self.status_bad}",
                f"DRDY flag bad     : {self.drdy_bad}",
                f"Sequence lost     : {self.seq_lost}",
                f"Backlog events    : {self.backlog_events}",
                f"Queue-drop hints  : {self.queue_drop_hints}",
                f"SPI read last/max : {self.last_read_us} / {self.max_read_us} us",
                f"Pending / Queue   : {self.last_pending} / {self.last_queue_depth}",
                f"BIAS_SENSP mask   : 0x{self.current_bias_mask():02X}",
                f"Selected RMS      : {rms}",
                f"Alpha peak        : {alpha_peak}",
                f"Alpha relative    : {alpha_rel}",
                f"50Hz power ratio  : {line_ratio}",
                f"Saturation        : {saturation:.4f}%",
                "",
                f"判断：{verdict}",
            ])
        )

    def make_verdict(self, saturation: float) -> str:
        if self.packet_count < FS * 2 and self.offline_uv is None:
            return "数据不足，至少采集 2 秒。"
        if self.parser.crc_bad > 0:
            return "有 CRC 错：先查串口缓存、帧格式或 USB CDC。"
        if self.status_bad > 0:
            return "ADS STATUS 异常：怀疑 SPI 位/字节错位。"
        if self.seq_lost > 0 or self.backlog_events > 0 or self.queue_drop_hints > 0:
            return "序号/DRDY backlog 异常：更像 MCU 实时采集或任务调度问题。"
        if np.isfinite(self.fs_est) and abs(self.fs_est - FS) > 2:
            return f"采样率 {self.fs_est:.1f} Hz 偏离 250 Hz。"
        if saturation > 0.1:
            return "有样本接近满量程：检查参考、BIAS、电极或前端饱和。"
        if np.isfinite(self.latest_line_ratio) and self.latest_line_ratio > 0.25:
            return "50 Hz 占比高：优先查 BIAS、SRB、接触阻抗、线缆和供电。"
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            delta = 10 * np.log10(max(self.closed_alpha, np.finfo(float).eps) / max(self.open_alpha, np.finfo(float).eps))
            return f"闭眼/睁眼 Alpha = {delta:+.2f} dB。"
        return "数字链路目前看起来健康；保存睁眼/闭眼窗后再看 Alpha。"

    def clear_stats(self):
        self.ring.clear()
        self.parser.reset()
        self.packet_count = 0
        self.status_bad = 0
        self.drdy_bad = 0
        self.seq_lost = 0
        self.backlog_events = 0
        self.queue_drop_hints = 0
        self.saturation_samples = 0
        self.last_seq = None
        self.last_queue_drop_low = 0
        self.first_seq = None
        self.first_clock = None
        self.fs_est = np.nan
        self.max_read_us = 0
        self.latest_alpha_power = np.nan
        self.latest_alpha_peak = np.nan
        self.latest_alpha_rel = np.nan
        self.latest_rms = np.nan
        self.latest_line_ratio = np.nan
        self.open_alpha = np.nan
        self.closed_alpha = np.nan
        self.set_status("统计已清空。")
        self.update_info_text()

    def closeEvent(self, event):  # noqa: N802
        try:
            self.stop_stream()
            if self.ser and self.ser.is_open:
                self.ser.close()
        finally:
            event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ADS1299 Native EEG GUI")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
