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
        self.append_values(frame.uv, frame.valid, frame.sequence, frame.mode)

    def append_values(self, values: np.ndarray, valid: bool, sequence: int, mode: int):
        self.data[:, self.head] = np.asarray(values, dtype=np.float32)
        self.valid[self.head] = bool(valid)
        self.seq[self.head] = np.uint32(sequence)
        self.mode[self.head] = np.uint8(mode)
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
        self.setWindowTitle("ADS1299 Native Python EEG GUI - P0+P1 filtering")
        self.resize(1500, 920)

        self.gain = 24
        self.lsb_uv = self.calc_lsb_uv()
        self.ring = RingBuffer(CHANNELS, FS * 90)            # untouched input-referred uV
        self.filtered_ring = RingBuffer(CHANNELS, FS * 90)   # continuous causal display chain
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

        # Display chain: continuous causal 1-40 Hz + narrow 50 Hz notch.
        self.sos_display_band = signal.butter(2, [1.0, 40.0], btype="bandpass", fs=FS, output="sos")
        notch_b, notch_a = signal.iirnotch(50.0, 30.0, fs=FS)
        self.sos_notch = signal.tf2sos(notch_b, notch_a)
        # Alpha chain is independent of GUI display switches. Offline/window analysis uses zero phase.
        self.sos_alpha_band = signal.butter(2, [1.0, 40.0], btype="bandpass", fs=FS, output="sos")
        self.psd_smooth_beta = 0.85
        self.reset_processing_state()

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
        self.psd_max_spin.setValue(40)
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
        self.reset_psd_smoothing()

    def reset_psd_smoothing(self, *_args):
        self.psd_smooth_f: Optional[np.ndarray] = None
        self.psd_smooth_db: Optional[np.ndarray] = None

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
            y = signal.sosfiltfilt(self.sos_notch, y)
        except ValueError:
            y = signal.sosfilt(self.sos_display_band, x)
            y = signal.sosfilt(self.sos_notch, y)
        return y

    def filter_for_alpha(self, x: np.ndarray) -> np.ndarray:
        x = signal.detrend(np.asarray(x, dtype=float), type="linear")
        if x.size < 64:
            return x
        try:
            y = signal.sosfiltfilt(self.sos_notch, x)
            y = signal.sosfiltfilt(self.sos_alpha_band, y)
        except ValueError:
            y = signal.sosfilt(self.sos_notch, x)
            y = signal.sosfilt(self.sos_alpha_band, y)
        return y

    def append_live_filtered(self, frames: List[Frame]):
        if not frames:
            return
        values = np.stack([fr.uv for fr in frames], axis=1).astype(float)
        valid = np.array([fr.valid for fr in frames], dtype=bool)
        filled = values.copy()
        for ch in range(CHANNELS):
            if not self.have_filter_input[ch]:
                first_candidates = np.flatnonzero(valid & np.isfinite(filled[ch]))
                if first_candidates.size:
                    first_idx = int(first_candidates[0])
                    first_value = float(filled[ch, first_idx])
                    filled[ch, :first_idx] = first_value
                    self.display_zi_band[ch] = signal.sosfilt_zi(self.sos_display_band) * first_value
                    self.display_zi_notch[ch].fill(0.0)
                    self.last_filter_input[ch] = first_value
                    self.have_filter_input[ch] = True
            for i in range(filled.shape[1]):
                if valid[i] and np.isfinite(filled[ch, i]):
                    self.last_filter_input[ch] = filled[ch, i]
                    self.have_filter_input[ch] = True
                else:
                    filled[ch, i] = self.last_filter_input[ch] if self.have_filter_input[ch] else 0.0
            y, self.display_zi_band[ch] = signal.sosfilt(
                self.sos_display_band, filled[ch], zi=self.display_zi_band[ch]
            )
            y, self.display_zi_notch[ch] = signal.sosfilt(
                self.sos_notch, y, zi=self.display_zi_notch[ch]
            )
            filled[ch] = y
        for i, fr in enumerate(frames):
            self.filtered_ring.append_values(filled[:, i], fr.valid, fr.sequence, fr.mode)

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

    def compute_alpha_from_window(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
    ) -> Tuple[bool, str, dict]:
        x = np.asarray(x, dtype=float)
        valid = np.asarray(valid, dtype=bool)
        self.latest_valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        cleaned_all, _gap_ok, _max_gap = self.clean_with_valid(x, valid, max_gap=2)
        metrics = {
            "cleaned": cleaned_all,
            "raw_f": np.array([]),
            "raw_p": np.array([]),
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

        # Raw diagnostic PSD remains completely unfiltered. It is intentionally separate
        # from the Alpha chain so a notch cannot hide a real 50 Hz hardware problem.
        raw_detrended = signal.detrend(cleaned_all, type="linear")
        nfft = max(2048, 2 ** int(np.ceil(np.log2(segment_len))))
        raw_f, raw_p = signal.welch(
            raw_detrended,
            fs=FS,
            window="hann",
            nperseg=segment_len,
            noverlap=3 * segment_len // 4,
            nfft=nfft,
        )
        metrics["raw_f"] = raw_f
        metrics["raw_p"] = raw_p

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
            alpha_signal = self.filter_for_alpha(segment_x)
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
            self.reset_processing_state()
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
            self.reset_processing_state()
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
                self.reset_processing_state()
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
        if live:
            self.append_live_filtered(frames)

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
            self.reset_processing_state()
            self.offline_uv = np.stack([f.uv for f in frames], axis=1).astype(np.float32)
            self.offline_valid = np.array([f.valid for f in frames], dtype=bool)
            self.offline_seq = np.array([f.sequence for f in frames], dtype=np.uint32)
            self.offline_mode = np.array([f.mode for f in frames], dtype=np.uint8)
            self.current_mode = int(self.offline_mode[-1])
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
        self.reset_psd_smoothing()
        if self.offline_uv is not None:
            self.offline_label.setText(f"{self.offline_end/FS:.1f}/{self.offline_uv.shape[1]/FS:.1f}s")
        self.update_fast_plots()

    def get_view_data(self, seconds: float, filtered_live: bool = False) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
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
        return source.latest(n)

    # ---------------- signal processing/plotting ----------------
    def prepare_plot_signal(self, x: np.ndarray, valid: np.ndarray, filtered_live: bool) -> np.ndarray:
        x = np.asarray(x, dtype=float).copy()
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

    def update_fast_plots(self):
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
        chain = "连续1-40Hz+50Hz陷波" if self.filter_check.isChecked() else "原始去均值"
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

    def update_psd_and_info(self):
        data, valid, seq, mode = self.get_view_data(10.0, filtered_live=False)
        self.latest_window_good = False
        self.latest_window_reason = "数据不足 4 秒"
        if data.shape[1] >= FS * 4:
            ch = self.channel_combo.currentIndex()
            x = data[ch].copy()
            good, reason, metrics = self.compute_alpha_from_window(x, valid, seq, mode)
            self.latest_window_good = good
            self.latest_window_reason = reason
            cleaned = metrics["cleaned"]
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
            else:
                plot_f, plot_p = alpha_f, alpha_p
                plot_name = "Alpha链 PSD（对数域平滑）"
            if plot_f.size:
                max_hz = float(self.psd_max_spin.value())
                smoothed_db = self.smooth_psd_db(plot_f, plot_p)
                mask = (plot_f >= 1) & (plot_f <= max_hz)
                self.psd_curve.setData(plot_f[mask], smoothed_db[mask])
                self.psd_plot.setXRange(1, max_hz, padding=0)

            if good:
                self.latest_alpha_power = metrics["alpha_power"]
                self.latest_alpha_peak = metrics["alpha_peak"]
                self.latest_alpha_rel = metrics["alpha_rel"]
                self.advance_alpha_capture()
            else:
                self.latest_alpha_power = np.nan
                self.latest_alpha_peak = np.nan
                self.latest_alpha_rel = np.nan
                self.advance_alpha_capture()

            peak_text = f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "无效"
            rel_text = f"{100*self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
            quality = "PASS" if good else f"REJECT: {reason}"
            self.psd_plot.setTitle(f"{plot_name} | Alpha {peak_text}, {rel_text} | {quality}")
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
        self.info_text.setPlainText(
            "\n".join([
                f"Mode              : {mode}",
                f"PGA / LSB         : {self.gain}x / {self.lsb_uv:.6g} uV/code",
                f"Streaming         : {int(self.streaming)}",
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
                "",
                f"Raw RMS           : {raw_rms}",
                f"Filtered RMS      : {filtered_rms}",
                f"Raw peak-to-peak  : {raw_pp}",
                f"Valid samples     : {valid_ratio}",
                f"Quality window    : {quality}",
                f"50Hz/raw ratio    : {line_ratio}",
                f"Alpha peak        : {alpha_peak}",
                f"Alpha relative    : {alpha_rel}",
                f"Alpha capture     : {capture}",
                f"Closed/Open Alpha : {comparison}",
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
            return "序号/DRDY backlog 异常：Alpha 坏窗会被丢弃，仍应先修 MCU 实时链路。"
        if np.isfinite(self.fs_est) and abs(self.fs_est - FS) > 2:
            return f"采样率 {self.fs_est:.1f} Hz 偏离 250 Hz。"
        if saturation > 0.1:
            return "有样本接近满量程：检查参考、BIAS、电极或前端饱和。"
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
