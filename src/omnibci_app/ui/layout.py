"""Layout behavior for the main window."""

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
    APP_ICON_PATH,
    CHANNELS,
    CHANNEL_COLORS,
    FS,
    MODE_ITEMS,
    OMNI_ORANGE,
    REFERENCE_ITEMS,
    VALID_GAINS,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class LayoutMixin:
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
        self.impedance_btn = QtWidgets.QPushButton("阻抗检测")
        self.impedance_btn.setToolTip("ADS1299 以 6 nA、31.25 Hz 激励并实时估算电极阻抗")
        self.impedance_btn.clicked.connect(self.open_impedance_dialog)
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
        self.psd_max_spin.setValue(65)
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
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID_minuteNN.bin（自动）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("每 60 秒自动切片；完整配置写入 manifest 和 .meta.json")
        controls.addWidget(self.bin_name, row, 1, 1, 4)
        self.import_btn = QtWidgets.QPushButton("导入文件")
        self.import_btn.clicked.connect(self.import_file)
        controls.addWidget(self.import_btn, row, 5)
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offline_slider.setEnabled(False)
        self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        controls.addWidget(self.offline_slider, row, 6, 1, 4)
        self.offline_label = QtWidgets.QLabel("未导入")
        controls.addWidget(self.offline_label, row, 10, 1, 2)

        row += 1
        bias_box = QtWidgets.QGroupBox("BIAS 通道：写入后读取 ADS1299 的 SENSP/SENSN 验证")
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
        self.bias_apply_btn = QtWidgets.QPushButton("写入并读回验证")
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
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#111111", width=2.2))
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


    def _build_omni_ui(self):
        """全域智能 compact review layout with acquisition diagnostics."""
        # Antialiasing eight continuously moving traces is expensive and adds
        # no useful EEG detail at screen resolution.
        pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#424245")
        self.setWindowTitle("全域智能 | ADS1299 EEG 工作站 | V18 | split BLE TX / capture pipeline")
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))
        self.setMinimumSize(1050, 680)
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#f4f5f7; color:#2d2521; font-size:12px; }
            QMenuBar { background:#ffffff; border-bottom:1px solid #d8dde3; padding:2px; }
            QMenuBar::item:selected, QMenu::item:selected { background:#fff0e6; color:#b83c00; }
            QToolBar { background:#ffffff; color:#2d2521; border-bottom:3px solid #ff5a01; spacing:5px; padding:5px 7px; min-height:48px; }
            QToolBar QLabel, QToolBar QCheckBox { color:#2d2521; background:transparent; font-size:13px; font-weight:600; }
            QToolBar QToolButton { color:#2d2521; background:transparent; border:0; font-weight:600; padding:4px 8px; }
            QToolBar QDoubleSpinBox, QToolBar QSpinBox, QToolBar QComboBox, QToolBar QPushButton {
                color:#2d2521; background:#ffffff; border:1px solid #d8dde3; min-height:22px;
            }
            QToolBar QComboBox QAbstractItemView { color:#2d2521; background:#ffffff; }
            QToolButton, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox {
                color:#2d2521; background:#ffffff; border:1px solid #d8dde3; padding:2px 5px; min-height:20px;
            }
            QToolButton:hover, QPushButton:hover { background:#fff4ed; border-color:#ff8b50; }
            QToolButton:pressed, QPushButton:pressed { background:#ffd8c2; }
            QGroupBox { background:#ffffff; color:#ff5a01; font-weight:600; border:1px solid #d8dde3; border-radius:3px; margin-top:7px; padding-top:6px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; background:#ffffff; }
            QTabWidget::pane { border:1px solid #d8dde3; background:#ffffff; }
            QTabBar::tab { background:#ffffff; border:1px solid #d8dde3; border-bottom:0; padding:5px 14px; }
            QTabBar::tab:selected { background:#fff0e6; color:#b83c00; border-color:#ff8b50; }
            QStatusBar { background:#ffffff; border-top:1px solid #d8dde3; }
            QDockWidget::title { background:#ffffff; color:#ff5a01; padding:5px; font-weight:600; }
            QDialog { background:#f4f5f7; }
            QDialog QLabel { color:#2d2521; }
        """)

        # File and view menus retain the existing acquisition functionality.
        file_menu = self.menuBar().addMenu("文件")
        open_action = file_menu.addAction("导入文件…")
        open_action.setShortcut(QtGui.QKeySequence.Open)
        open_action.triggered.connect(self.import_file)
        export_action = file_menu.addAction("导出 CSV…")
        export_action.setShortcut("Ctrl+Shift+S")
        export_action.triggered.connect(self.export_csv)
        format_action = file_menu.addAction("导出 BDF/FIF…")
        format_action.triggered.connect(self.export_biosignal_formats)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close, QtGui.QKeySequence.Quit)
        view_menu = self.menuBar().addMenu("视图")
        mne_action = view_menu.addAction("打开 MNE 浏览器")
        mne_action.triggered.connect(self.open_mne_browser)
        acquire_menu = self.menuBar().addMenu("采集")
        acquire_menu.addAction("连接/断开设备", self.toggle_connection)
        acquire_menu.addAction("开始采集并保存 BIN", self.start_stream)
        acquire_menu.addAction(
            "停止采集", lambda: self.stop_stream(offer_export=True)
        )

        toolbar = self.addToolBar("EEG 工具")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.logo_label = QtWidgets.QLabel()
        self.logo_label.setToolTip("全域智能")
        self.logo_label.setFixedSize(220, 54)
        self.logo_label.setAlignment(QtCore.Qt.AlignCenter)
        # Compose the EEG identity inside the original 220 x 54 logo slot so
        # the P0P1 toolbar geometry and every surrounding control stay put.
        logo_canvas = QtGui.QPixmap(220, 54)
        logo_canvas.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(logo_canvas)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        mark = QtGui.QPixmap(str(APP_ICON_PATH))
        if not mark.isNull():
            painter.drawPixmap(
                4, 4, mark.scaled(
                    46, 46, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
                )
            )
        painter.setPen(QtGui.QColor("#1d1d1f"))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 12, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(57, 5, 155, 25), QtCore.Qt.AlignLeft, "全域智能")
        painter.setPen(QtGui.QColor(OMNI_ORANGE))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 9, QtGui.QFont.DemiBold))
        painter.drawText(
            QtCore.QRect(57, 28, 155, 20),
            QtCore.Qt.AlignLeft,
            "脑电测试 · EEG",
        )
        painter.end()
        self.logo_label.setPixmap(logo_canvas)
        toolbar.addWidget(self.logo_label)
        toolbar.addSeparator()
        toolbar.addAction(open_action)
        export_file_button = QtWidgets.QToolButton()
        export_file_button.setText("导出文件…")
        export_file_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        export_file_menu = QtWidgets.QMenu(export_file_button)
        export_file_menu.addAction(export_action)
        export_file_menu.addAction(format_action)
        export_file_button.setMenu(export_file_menu)
        toolbar.addWidget(export_file_button)
        toolbar.addSeparator()

        self.filter_check = QtWidgets.QCheckBox("滤波后")
        self.filter_check.setChecked(True)
        self.filter_check.stateChanged.connect(self._filter_settings_changed)
        toolbar.addWidget(self.filter_check)
        toolbar.addSeparator()
        self.start_time_spin = QtWidgets.QDoubleSpinBox()
        self.start_time_spin.setRange(0, 86400)
        self.start_time_spin.setDecimals(1)
        self.start_time_spin.setSuffix(" s")
        self.start_time_spin.setKeyboardTracking(False)
        self.start_time_spin.valueChanged.connect(self._start_time_changed)
        self._add_tool_field(toolbar, "开始时间", self.start_time_spin)
        self.win_spin = QtWidgets.QDoubleSpinBox()
        self.win_spin.setRange(1, 60)
        self.win_spin.setValue(10)
        self.win_spin.setSuffix(" s")
        self.win_spin.valueChanged.connect(self._window_changed)
        self._add_tool_field(toolbar, "时间窗", self.win_spin)
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItems(["15", "30", "60"])
        self.speed_combo.setCurrentText("30")
        self._add_tool_field(toolbar, "走纸速度", self.speed_combo, " mm/s")
        self.sensitivity_spin = QtWidgets.QDoubleSpinBox()
        self.sensitivity_spin.setRange(1, 100000)
        self.sensitivity_spin.setValue(100)
        self.sensitivity_spin.setSuffix(" uV/cm")
        self.sensitivity_spin.valueChanged.connect(self.update_fast_plots)
        self._add_tool_field(toolbar, "灵敏度", self.sensitivity_spin)
        self.hp_spin = QtWidgets.QDoubleSpinBox()
        self.hp_spin.setRange(0.1, 30); self.hp_spin.setValue(5); self.hp_spin.setSuffix(" Hz")
        self.lp_spin = QtWidgets.QDoubleSpinBox()
        self.lp_spin.setRange(10, 120); self.lp_spin.setValue(50); self.lp_spin.setSuffix(" Hz")
        self.notch_check = QtWidgets.QCheckBox("50/100 Hz 谐波陷波"); self.notch_check.setChecked(True)
        self.notch_check.setToolTip(
            "级联抑制 50 Hz 和 100 Hz；采样率为 250 SPS 时，150 Hz 会混叠到 100 Hz。"
        )
        self.hp_spin.valueChanged.connect(self._filter_settings_changed)
        self.lp_spin.valueChanged.connect(self._filter_settings_changed)
        self.notch_check.stateChanged.connect(self._filter_settings_changed)
        toolbar.addSeparator()
        toolbar.addAction(mne_action)

        # USB CDC and BLE share one acquisition/parser pipeline.
        self.transport_combo = QtWidgets.QComboBox()
        self.transport_combo.addItem("USB 串口", "serial")
        self.transport_combo.addItem("BLE 无线", "ble")
        self.transport_combo.setMinimumWidth(105)
        self.transport_combo.currentIndexChanged.connect(self.transport_mode_changed)
        self.serial_label = QtWidgets.QLabel("串口")
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(220)
        self.port_combo.setToolTip("先扫描设备，再选择要连接的目标")
        self.refresh_btn = QtWidgets.QPushButton("扫描串口")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn = QtWidgets.QPushButton("打开串口")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.start_btn = QtWidgets.QPushButton("开始采集")
        self.start_btn.clicked.connect(self.start_stream)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.clicked.connect(lambda: self.stop_stream(offer_export=True))
        self.impedance_btn = QtWidgets.QPushButton("阻抗检测")
        self.impedance_btn.setToolTip(
            "ADS1299 以 6 nA、31.25 Hz 激励并实时估算电极阻抗"
        )
        self.impedance_btn.clicked.connect(self.open_impedance_dialog)
        self.internal_short_btn = QtWidgets.QPushButton("内部短接")
        self.internal_short_btn.setCheckable(True)
        self.internal_short_btn.setToolTip(
            "一键把所有已启用 ADS1299 通道切到内部输入短接（MUX=001）；"
            "再次点击会恢复进入短接前的 EEG/BIAS 模式。"
        )
        self.internal_short_btn.toggled.connect(self.toggle_internal_short)
        self.reference_combo = QtWidgets.QComboBox()
        for label, value in REFERENCE_ITEMS:
            self.reference_combo.addItem(label, value)
        self.reference_combo.setCurrentIndex(0)
        self.reference_combo.setMinimumWidth(205)
        self.reference_combo.setToolTip(
            "SRB1：每通道信号接 INxP，参考接 SRB1；"
            "SRB2：每通道信号接 INxN，参考接 SRB2。"
        )
        self.apply_reference_btn = QtWidgets.QPushButton("应用参考")
        self.apply_reference_btn.clicked.connect(self.apply_reference_mode)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(5, 5, 5, 3)
        layout.setSpacing(3)

        serial_box = QtWidgets.QGroupBox("设备连接与控制")
        serial_layout = QtWidgets.QHBoxLayout(serial_box)
        serial_layout.setContentsMargins(8, 4, 8, 4)
        serial_layout.addWidget(self.transport_combo)
        serial_layout.addWidget(self.serial_label)
        serial_layout.addWidget(self.port_combo, 1)
        serial_layout.addWidget(self.refresh_btn)
        serial_layout.addWidget(self.connect_btn)
        serial_layout.addWidget(self.start_btn)
        serial_layout.addWidget(self.stop_btn)
        serial_layout.addWidget(self.impedance_btn)
        serial_layout.addWidget(self.internal_short_btn)
        serial_layout.addWidget(QtWidgets.QLabel("参考"))
        serial_layout.addWidget(self.reference_combo)
        serial_layout.addWidget(self.apply_reference_btn)
        self.status_label = QtWidgets.QLabel("未扫描")
        self.status_label.setStyleSheet("color:#c94700; font-weight:600;")
        serial_layout.addWidget(self.status_label, 1)
        layout.addWidget(serial_box)

        filter_box = QtWidgets.QGroupBox("滤波设置")
        filter_layout = QtWidgets.QHBoxLayout(filter_box)
        filter_layout.setContentsMargins(8, 4, 8, 4)
        filter_layout.addWidget(QtWidgets.QLabel("高通"))
        self.hp_spin.setMinimumWidth(90)
        filter_layout.addWidget(self.hp_spin)
        filter_layout.addWidget(QtWidgets.QLabel("低通"))
        self.lp_spin.setMinimumWidth(90)
        filter_layout.addWidget(self.lp_spin)
        filter_layout.addWidget(self.notch_check)
        filter_layout.addStretch(1)
        layout.addWidget(filter_box)

        self.view_tabs = QtWidgets.QTabWidget()
        wave_page = QtWidgets.QWidget()
        wave_page_layout = QtWidgets.QVBoxLayout(wave_page)
        wave_page_layout.setContentsMargins(2, 2, 2, 2)
        wave_page_layout.setSpacing(3)
        self.view_tabs.addTab(wave_page, "波形")

        # A dedicated, full-size single-channel view. Double-clicking any
        # channel plot below selects the channel and switches to this tab.
        self.single_channel_index = 0
        single_page = QtWidgets.QWidget()
        single_layout = QtWidgets.QVBoxLayout(single_page)
        single_layout.setContentsMargins(5, 5, 5, 5)
        single_header = QtWidgets.QHBoxLayout()
        single_header.addWidget(QtWidgets.QLabel("放大通道"))
        self.single_channel_combo = QtWidgets.QComboBox()
        self.single_channel_combo.addItems([f"CH{i}" for i in range(1, CHANNELS + 1)])
        self.single_channel_combo.currentIndexChanged.connect(self._single_channel_changed)
        single_header.addWidget(self.single_channel_combo)
        self.single_channel_status = QtWidgets.QLabel("CH1 | 等待数据")
        self.single_channel_status.setStyleSheet("color:#c94700;font-weight:600;")
        single_header.addWidget(self.single_channel_status, 1)
        single_header.addWidget(QtWidgets.QLabel("纵轴 Scale"))
        self.single_scale_spin = QtWidgets.QDoubleSpinBox()
        self.single_scale_spin.setRange(1.0, 100000.0)
        self.single_scale_spin.setDecimals(0)
        self.single_scale_spin.setValue(100.0)
        self.single_scale_spin.setSuffix(" µV")
        self.single_scale_spin.setKeyboardTracking(False)
        self.single_scale_spin.setToolTip(
            "当前放大通道的纵轴半幅；也可在波形上滚动鼠标滚轮调整"
        )
        self.single_scale_spin.valueChanged.connect(self._single_scale_changed)
        single_header.addWidget(self.single_scale_spin)
        back_to_all = QtWidgets.QPushButton("返回八通道")
        back_to_all.clicked.connect(lambda: self.view_tabs.setCurrentIndex(0))
        single_header.addWidget(back_to_all)
        single_layout.addLayout(single_header)
        self.single_plot = pg.PlotWidget(axisItems={"bottom": ClockAxisItem(orientation="bottom")})
        self.single_plot.setBackground("#ffffff")
        self.single_plot.setMenuEnabled(False)
        self.single_plot.setMouseEnabled(x=True, y=False)
        self._scale_viewbox_channels = {}
        self._scale_viewbox_channels[self.single_plot.getViewBox()] = -1
        self.single_plot.getViewBox().installEventFilter(self)
        self.single_plot.showGrid(x=True, y=True, alpha=0.22)
        self.single_plot.setLabel("left", "幅值", units="uV")
        self.single_plot.setLabel("bottom", "时间")
        self.single_zero_line = self.single_plot.addLine(
            y=0, pen=pg.mkPen("#56616b", width=1)
        )
        self.single_curve = self.single_plot.plot(
            pen=pg.mkPen(CHANNEL_COLORS[0], width=2.4), connect="finite"
        )
        self.single_curve.setClipToView(True)
        self.single_curve.setDownsampling(auto=True, method="peak")
        single_layout.addWidget(self.single_plot, 1)
        self.single_nav_plot = pg.PlotWidget()
        self.single_nav_plot.setFixedHeight(62)
        self.single_nav_plot.hideAxis("left")
        self.single_nav_plot.setMouseEnabled(x=True, y=False)
        self.single_nav_plot.getPlotItem().setMenuEnabled(False)
        self.single_nav_plot.setBackground("#ffffff")
        self.single_nav_curve = self.single_nav_plot.plot(
            pen=pg.mkPen("#86868b", width=1)
        )
        self.single_nav_region = pg.LinearRegionItem(
            values=(0, 10),
            movable=True,
            brush=pg.mkBrush(255, 90, 1, 45),
            pen=pg.mkPen(OMNI_ORANGE, width=1.5),
        )
        self.single_nav_region.sigRegionChanged.connect(self._nav_region_changed)
        self.single_nav_plot.addItem(self.single_nav_region)
        self.single_nav_plot.setVisible(False)
        single_layout.addWidget(self.single_nav_plot)
        self.single_tab_index = self.view_tabs.addTab(single_page, "单通道放大")
        self.view_tabs.currentChanged.connect(self.update_fast_plots)
        layout.addWidget(self.view_tabs, 1)

        self.nav_plot = pg.PlotWidget()
        self.nav_plot.setFixedHeight(62)
        self.nav_plot.hideAxis("left")
        self.nav_plot.setMouseEnabled(x=True, y=False)
        self.nav_plot.getPlotItem().setMenuEnabled(False)
        self.nav_plot.setBackground("#ffffff")
        self.nav_curve = self.nav_plot.plot(pen=pg.mkPen("#86868b", width=1))
        self.nav_region = pg.LinearRegionItem(values=(0, 10), movable=True,
            brush=pg.mkBrush(255, 90, 1, 45), pen=pg.mkPen(OMNI_ORANGE, width=1.5))
        self.nav_region.sigRegionChanged.connect(self._nav_region_changed)
        self.nav_plot.addItem(self.nav_region)
        wave_page_layout.addWidget(self.nav_plot)

        self.event_label = QtWidgets.QLabel("  ━  滤波显示副本（不修改原始数据）")
        self.event_label.setFixedHeight(21)
        self.event_label.setStyleSheet("background:#fff0e6;color:#b83c00;border:1px solid #f3c2a5;")
        wave_page_layout.addWidget(self.event_label)

        wave_row = QtWidgets.QWidget()
        wave_layout = QtWidgets.QHBoxLayout(wave_row)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        wave_layout.setSpacing(0)
        channel_panel = QtWidgets.QFrame()
        channel_panel.setFixedWidth(285)
        channel_panel.setStyleSheet("background:#ffffff;border-right:1px solid #d8dde3;")
        channel_layout = QtWidgets.QVBoxLayout(channel_panel)
        channel_layout.setContentsMargins(0, 0, 0, 31)
        channel_layout.setSpacing(0)
        channel_header = QtWidgets.QLabel("通道参数（点击通道修改） / 幅值")
        channel_header.setAlignment(QtCore.Qt.AlignCenter)
        channel_header.setFixedHeight(27)
        channel_header.setStyleSheet("background:#ffffff;color:#ff5a01;border-bottom:3px solid #ff5a01;font-size:14px;font-weight:bold;")
        channel_layout.addWidget(channel_header)
        self.channel_buttons = []
        self.channel_scales = []
        for ch in range(CHANNELS):
            row_widget = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 2, 0)
            row_layout.setSpacing(2)
            button = QtWidgets.QToolButton()
            button.setText(f"CH{ch+1}")
            status_icon = QtGui.QPixmap(11, 11)
            status_icon.fill(QtGui.QColor("#56bd31"))
            button.setIcon(QtGui.QIcon(status_icon))
            button.setIconSize(QtCore.QSize(11, 11))
            button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
            button.setStyleSheet(
                "QToolButton{background:#ffffff;color:#2d2521;border:0;"
                "border-bottom:1px solid #e2e6eb;text-align:left;padding-left:10px;font-size:13px;}"
            )
            button.clicked.connect(lambda _checked=False, index=ch: self.open_channel_settings(index))
            row_layout.addWidget(button, 1)
            self.channel_buttons.append(button)
            scale = QtWidgets.QDoubleSpinBox()
            scale.setRange(1.0, 100000.0)
            scale.setDecimals(0)
            scale.setValue(100.0)
            scale.setSuffix(" µV")
            scale.setToolTip(f"CH{ch+1} 独立纵轴半幅；最大 100000 µV = 0.1 V")
            scale.setKeyboardTracking(False)
            scale.valueChanged.connect(
                lambda value, index=ch: self._channel_scale_changed(index, value)
            )
            row_layout.addWidget(scale)
            self.channel_scales.append(scale)
            channel_layout.addWidget(row_widget, 1)
        self.refresh_channel_parameter_labels()
        wave_layout.addWidget(channel_panel)

        # Each channel gets its own PlotItem and y-range.  This removes the
        # artificial lane offsets and lets every channel use its own amplitude.
        self.wave_widget = pg.GraphicsLayoutWidget()
        self.wave_widget.setBackground("#ffffff")
        self.channel_plots = []
        self.stack_curves = []
        for ch in range(CHANNELS):
            plot = self.wave_widget.addPlot(row=ch, col=0)
            plot.setMenuEnabled(False)
            plot.setMouseEnabled(x=True, y=False)
            if ch > 0:
                plot.setXLink(self.channel_plots[0])
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setLabel("left", f"CH{ch+1}", units="uV")
            if ch < CHANNELS - 1:
                plot.hideAxis("bottom")
            else:
                plot.setLabel("bottom", "时间", units="s")
            curve = plot.plot(pen=pg.mkPen(CHANNEL_COLORS[ch], width=2.0),
                              connect="finite")
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
            self.channel_plots.append(plot)
            self.stack_curves.append(curve)
            self._scale_viewbox_channels[plot.getViewBox()] = ch
            plot.getViewBox().installEventFilter(self)
        self.stack_plot = self.channel_plots[0]
        self.stack_plot.getViewBox().sigXRangeChanged.connect(self._main_range_changed)
        self.wave_widget.scene().sigMouseClicked.connect(self._wave_scene_clicked)
        wave_layout.addWidget(self.wave_widget, 1)
        wave_page_layout.addWidget(wave_row, 1)

        # Compatibility widgets used by the existing acquisition/PSD code.
        # The serial widgets above are the real, visible controls; do not
        # recreate them here or the toolbar would lose its signal bindings.
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID_minuteNN.bin（自动）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("每 60 秒自动切片；完整配置写入 manifest 和 .meta.json")
        self.mode_combo = QtWidgets.QComboBox()
        for name, _, _ in MODE_ITEMS: self.mode_combo.addItem(name)
        self.pga_combo = QtWidgets.QComboBox(); self.pga_combo.addItems([str(x) for x in VALID_GAINS]); self.pga_combo.setCurrentText("24")
        self.channel_combo = QtWidgets.QComboBox(); self.channel_combo.addItems([f"CH{i}" for i in range(1, 9)])
        self.channel_combo.currentIndexChanged.connect(self.reset_psd_smoothing)
        self.psd_raw_check = QtWidgets.QCheckBox("显示未滤波 PSD")
        self.psd_raw_check.setChecked(False)
        self.psd_raw_check.stateChanged.connect(self.reset_psd_smoothing)
        self.psd_max_spin = QtWidgets.QDoubleSpinBox()
        self.psd_max_spin.setRange(10.0, FS / 2)
        self.psd_max_spin.setValue(65.0)
        self.psd_max_spin.setSingleStep(5.0)
        self.psd_max_spin.setSuffix(" Hz")
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        self.offline_label = QtWidgets.QLabel()
        self.bias_checks = [QtWidgets.QCheckBox() for _ in range(8)]
        for i, cb in enumerate(self.bias_checks): cb.setChecked(i < 5)
        self.bias_mask_label = QtWidgets.QLabel()
        self.open_btn = QtWidgets.QPushButton(); self.closed_btn = QtWidgets.QPushButton()
        self.time_plot = pg.PlotWidget(); self.time_curve = self.time_plot.plot()
        self.psd_plot = pg.PlotWidget()
        self.psd_plot.setBackground("#ffffff")
        self.psd_plot.showGrid(x=True, y=True, alpha=0.22)
        self.psd_plot.setLabel("bottom", "频率", units="Hz")
        self.psd_plot.setLabel("left", "PSD", units="dB µV²/Hz")
        self.psd_plot.setTitle("Welch PSD | 等待数据")
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#111111", width=2.2))
        self.psd_plot.addLine(x=8, pen=pg.mkPen("#ff9a5c", style=QtCore.Qt.DashLine))
        self.psd_plot.addLine(x=13, pen=pg.mkPen("#ff9a5c", style=QtCore.Qt.DashLine))
        psd_page = QtWidgets.QWidget()
        psd_layout = QtWidgets.QVBoxLayout(psd_page)
        psd_layout.setContentsMargins(6, 6, 6, 6)
        psd_controls = QtWidgets.QHBoxLayout()
        psd_controls.addWidget(QtWidgets.QLabel("通道"))
        self.channel_combo.setMinimumWidth(90)
        psd_controls.addWidget(self.channel_combo)
        psd_controls.addWidget(self.psd_raw_check)
        psd_controls.addWidget(QtWidgets.QLabel("频率上限"))
        self.psd_max_spin.setMinimumWidth(100)
        psd_controls.addWidget(self.psd_max_spin)
        psd_controls.addStretch(1)
        psd_controls.addWidget(QtWidgets.QLabel("橙色虚线：8–13 Hz Alpha 范围"))
        psd_layout.addLayout(psd_controls)
        psd_layout.addWidget(self.psd_plot, 1)
        # PSD is a movable/floating dock so it can sit beside the waveform or
        # be pulled out as a separate window, like a detachable browser tab.
        self.psd_dock = QtWidgets.QDockWidget("频谱 PSD（可拖动/浮动）", self)
        self.psd_dock.setObjectName("PsdDock")
        self.psd_dock.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea
            | QtCore.Qt.RightDockWidgetArea
            | QtCore.Qt.BottomDockWidgetArea
        )
        self.psd_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFloatable
            | QtWidgets.QDockWidget.DockWidgetClosable
        )
        self.psd_dock.setWidget(psd_page)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.psd_dock)
        self.psd_toggle_action = view_menu.addAction("显示/隐藏 PSD 窗口")
        self.psd_toggle_action.setCheckable(True)
        self.psd_toggle_action.setChecked(True)
        self.psd_toggle_action.triggered.connect(self.toggle_psd_dock)
        self.psd_dock.visibilityChanged.connect(self.psd_toggle_action.setChecked)

        # Transport diagnostics used to be instantiated but never inserted into
        # the Omni layout.  Give it a real tab so it is always reachable without
        # shrinking, clipping, or otherwise altering the waveform views.
        diagnostics_page = QtWidgets.QWidget()
        diagnostics_layout = QtWidgets.QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(6, 6, 6, 6)
        diagnostics_layout.setSpacing(4)

        diagnostics_header = QtWidgets.QHBoxLayout()
        diagnostics_title = QtWidgets.QLabel("传输与显示诊断")
        diagnostics_title.setStyleSheet("font-size:14px; font-weight:700; color:#ff5a01;")
        diagnostics_header.addWidget(diagnostics_title)
        diagnostics_header.addStretch(1)
        self.diagnostics_pause_btn = QtWidgets.QPushButton("暂停刷新")
        self.diagnostics_pause_btn.setCheckable(True)
        self.diagnostics_pause_btn.setToolTip("暂停后数值和滚动位置保持不变，便于截图或抄录")
        self.diagnostics_pause_btn.toggled.connect(
            lambda checked: self.diagnostics_pause_btn.setText(
                "继续刷新" if checked else "暂停刷新"
            )
        )
        diagnostics_header.addWidget(self.diagnostics_pause_btn)
        diagnostics_copy_btn = QtWidgets.QPushButton("复制诊断信息")
        diagnostics_copy_btn.setToolTip("复制当前全部诊断字段，便于排查 BLE/GUI 卡顿")
        diagnostics_header.addWidget(diagnostics_copy_btn)
        diagnostics_layout.addLayout(diagnostics_header)

        diagnostics_note = QtWidgets.QLabel(
            "三栏紧凑显示；诊断每 0.5 秒刷新一次，不改变滚动位置，也不影响 EEG 数据。"
        )
        diagnostics_note.setWordWrap(False)
        diagnostics_note.setStyleSheet("color:#5b6168;font-size:11px;")
        diagnostics_layout.addWidget(diagnostics_note)

        self.info_text = QtWidgets.QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.info_text.setStyleSheet(
            "QPlainTextEdit{background:#ffffff;color:#242424;border:1px solid #d8dde3;"
            "font-family:Consolas, 'Cascadia Mono', monospace;font-size:10px;padding:4px;}"
        )
        self.info_text.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.info_text.document().setDocumentMargin(2)
        diagnostics_layout.addWidget(self.info_text, 1)
        diagnostics_copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(self.info_text.toPlainText())
        )
        self.diagnostics_tab_index = self.view_tabs.addTab(diagnostics_page, "传输诊断")
        diagnostics_action = view_menu.addAction("打开传输诊断页")
        diagnostics_action.triggered.connect(
            lambda: self.view_tabs.setCurrentIndex(self.diagnostics_tab_index)
        )

        self.yrange_spin = QtWidgets.QDoubleSpinBox(); self.yrange_spin.setValue(200)
        self.file_status = QtWidgets.QLabel("未打开文件")
        self.range_status = QtWidgets.QLabel("0.0–0.0 s")
        self.filter_status = QtWidgets.QLabel("5–50 Hz + 50/100 Hz harmonic notch")
        self.statusBar().addWidget(self.file_status, 1)
        self.statusBar().addPermanentWidget(self.range_status)
        self.statusBar().addPermanentWidget(self.filter_status)
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Left), self, activated=lambda: self.page(-1))
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Right), self, activated=lambda: self.page(1))


    def _add_tool_field(self, toolbar, label, widget, trailing=""):
        toolbar.addWidget(QtWidgets.QLabel(label + " "))
        toolbar.addWidget(widget)
        if trailing:
            toolbar.addWidget(QtWidgets.QLabel(trailing))


    def toggle_psd_dock(self, visible: bool):
        if hasattr(self, "psd_dock"):
            self.psd_dock.setVisible(bool(visible))


    def _filter_settings_changed(self, *_):
        hp, lp = float(self.hp_spin.value()), float(self.lp_spin.value())
        if hp >= lp:
            return
        self.sos_display_band = signal.butter(2, [hp, lp], btype="bandpass", fs=FS, output="sos")
        self.reset_processing_state()
        self.event_label.setVisible(self.filter_check.isChecked())
        self.update_fast_plots()


    def _total_samples(self):
        return (
            self.offline_uv.shape[1]
            if self.offline_uv is not None
            else int(self.ring.total_appended)
        )


    def _start_time_changed(self, value):
        if self.offline_uv is not None:
            self.offline_end = min(self._total_samples(), int((value + self.win_spin.value()) * FS))
            self.reset_psd_smoothing()
        self.update_fast_plots()


    def _window_changed(self, *_):
        if self.offline_uv is not None:
            self.offline_end = min(
                self._total_samples(),
                int((self.start_time_spin.value() + self.win_spin.value()) * FS),
            )
            self.reset_psd_smoothing()
        self.update_fast_plots()


    def _nav_region_changed(self, region=None):
        if getattr(self, "_syncing_nav", False):
            return
        active_region = region if region is not None else self.nav_region
        lo, hi = active_region.getRegion()
        self.win_spin.blockSignals(True); self.start_time_spin.blockSignals(True)
        self.win_spin.setValue(max(1, hi - lo)); self.start_time_spin.setValue(max(0, lo))
        self.win_spin.blockSignals(False); self.start_time_spin.blockSignals(False)
        if self.offline_uv is not None:
            self.offline_end = min(self._total_samples(), int(hi * FS))
            self.reset_psd_smoothing()
        self.update_fast_plots()


    def page(self, direction):
        total = self._total_samples() / FS
        width = self.win_spin.value()
        self.start_time_spin.setValue(max(0, min(max(0, total-width),
                                                  self.start_time_spin.value() + direction*width)))


    def eventFilter(self, obj, event):
        if (
            event.type() == QtCore.QEvent.Wheel
            and obj in getattr(self, "_scale_viewbox_channels", {})
        ):
            channel = self._scale_viewbox_channels[obj]
            if channel < 0:
                channel = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
            factor = 0.8 if event.angleDelta().y() > 0 else 1.25
            scale = self.channel_scales[channel]
            scale.setValue(
                float(np.clip(scale.value() * factor, scale.minimum(), scale.maximum()))
            )
            return True
        return super().eventFilter(obj, event)


    def _channel_scale_changed(self, channel: int, value: float):
        if (
            hasattr(self, "single_scale_spin")
            and int(channel) == int(self.single_channel_index)
        ):
            self.single_scale_spin.blockSignals(True)
            self.single_scale_spin.setValue(float(value))
            self.single_scale_spin.blockSignals(False)
        self.update_fast_plots()


    def _single_scale_changed(self, value: float):
        if not hasattr(self, "channel_scales"):
            return
        channel = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
        self.channel_scales[channel].setValue(float(value))


    def _plot_clicked(self, event):
        pos = self.stack_plot.getPlotItem().vb.mapSceneToView(event.scenePos())
        sensitivity = self.sensitivity_spin.value()
        ch = int(round((7.5 * sensitivity - pos.y()) / (2 * sensitivity)))
        if 0 <= ch < CHANNELS:
            self._select_channel(ch)


    def _wave_scene_clicked(self, event):
        """Select on one click; open the isolated channel tab on double-click."""
        if event.button() != QtCore.Qt.LeftButton:
            return
        scene_pos = event.scenePos()
        channel = None
        for ch, plot in enumerate(self.channel_plots):
            if plot.sceneBoundingRect().contains(scene_pos):
                channel = ch
                break
        if channel is None:
            return
        self._select_channel(channel)
        is_double = bool(event.double()) if hasattr(event, "double") else False
        if is_double:
            self.show_single_channel(channel)
            event.accept()


    def show_single_channel(self, channel: int):
        channel = int(np.clip(channel, 0, CHANNELS - 1))
        self.single_channel_index = channel
        self.single_channel_combo.blockSignals(True)
        self.single_channel_combo.setCurrentIndex(channel)
        self.single_channel_combo.blockSignals(False)
        self.single_scale_spin.blockSignals(True)
        self.single_scale_spin.setValue(self.channel_scales[channel].value())
        self.single_scale_spin.blockSignals(False)
        self.view_tabs.setCurrentIndex(self.single_tab_index)
        self.update_fast_plots()
        self.set_status(
            f"CH{channel+1} 已在“单通道放大”Tab 中独立显示；"
            "双击其他通道可直接切换。"
        )


    def _single_channel_changed(self, channel: int):
        if channel < 0:
            return
        self.single_channel_index = int(channel)
        self.single_scale_spin.blockSignals(True)
        self.single_scale_spin.setValue(self.channel_scales[self.single_channel_index].value())
        self.single_scale_spin.blockSignals(False)
        self._select_channel(self.single_channel_index)
        self.update_fast_plots()


    def _select_channel(self, ch):
        self.channel_combo.setCurrentIndex(ch)
        for i, curve in enumerate(self.stack_curves):
            curve.setPen(pg.mkPen(
                CHANNEL_COLORS[i],
                width=3.0 if i == ch else 2.0,
            ))
        self.single_curve.setPen(pg.mkPen(CHANNEL_COLORS[ch], width=2.4))
        if hasattr(self, "single_scale_spin"):
            self.single_scale_spin.blockSignals(True)
            self.single_scale_spin.setValue(self.channel_scales[ch].value())
            self.single_scale_spin.blockSignals(False)
        for i, button in enumerate(self.channel_buttons):
            button.setStyleSheet(
                "QToolButton{"
                f"background:{'#fff0e6' if i == ch else '#ffffff'};"
                "color:#2d2521;border:0;border-bottom:1px solid #e2e6eb;"
                "text-align:left;padding-left:10px;font-size:13px;"
                f"font-weight:{'bold' if i == ch else 'normal'};"
                "}"
            )
