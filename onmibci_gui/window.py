"""OmniBCI Qt main window and UI composition."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace
from .channel_config import ChannelConfigMixin
from .exports import ExportMixin
from .transport_control import TransportControlMixin
from .acquisition import AcquisitionMixin
from .display import DisplayMixin


class MainWindow(
    ChannelConfigMixin,
    ExportMixin,
    TransportControlMixin,
    AcquisitionMixin,
    DisplayMixin,
    QtWidgets.QMainWindow,
):
    api_gui_request = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            f"全域智能 | OmniBCI V{APP_RELEASE_VERSION} | "
            "ADS1299 EEG 工作站 | 固件 V19 / 通信协议 V1"
        )
        self.resize(1500, 920)
        self.event_logger = AsyncEventLogger(LOG_DIR)
        self._last_logged_status = ""
        self._last_render_stall_log_monotonic = 0.0
        self._last_periodic_perf_log_monotonic = 0.0
        self._render_stall_times = deque(maxlen=32)
        self._self_repair_until = 0.0
        self._self_repair_active = False
        self._last_user_action_text = "app_start"
        self._last_user_action_monotonic = time.monotonic()
        self.event_logger.log(
            "app_start",
            python=sys.version.split()[0],
            frozen=IS_FROZEN,
            log_file=str(self.event_logger.path),
        )

        self.gain = 24  # legacy/global command value
        self.channel_gains = np.full(CHANNELS, 24, dtype=np.int16)
        self.app_settings = QtCore.QSettings("OmniBCI", "ADS1299EEGWorkbench")
        self.channel_names = self._load_channel_names()
        self.channel_enabled = np.array([True] * 5 + [False] * 3, dtype=bool)
        self.channel_bias = np.array([True] * 5 + [False] * 3, dtype=bool)
        # The GUI intentionally exposes only the fixed SRB1 wiring profile:
        # measurement electrodes on INxP and the common reference on SRB1.
        self.reference_mode = REFERENCE_SRB1
        self.channel_srb2 = np.zeros(CHANNELS, dtype=bool)
        self.lsb_uv = self.calc_lsb_uv()
        self.ring = RingBuffer(CHANNELS, FS * 90)  # untouched input-referred uV
        self.filtered_ring = RingBuffer(CHANNELS, FS * 90)  # continuous causal display chain
        self.parser = AdsFrameParser(self.channel_lsb_uv)
        self.filter_generation = 0
        self.filter_worker: Optional[LiveFilterWorker] = None
        self.filter_batches_applied = 0
        self.filter_stale_batches = 0
        self.stream_server: Optional[LocalStreamServer] = None
        self.api_gui_request.connect(self._handle_api_gui_request)
        self.stream_api_errors = 0
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
        self.psd_active_worker: Optional[PsdWorker] = None
        self.psd_request_id = 0
        self.psd_last_signature = None
        self._last_nav_update = 0.0
        self.filter_worker = LiveFilterWorker(self.sos_display_band, self.sos_notch, True)
        self.filter_worker.start()
        self.reset_processing_state()

        self._build_omni_ui()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.stream_server = LocalStreamServer(
            stop_handler=self._api_stop_measurement,
            export_handler=self._api_export_bdf,
        )
        try:
            self.stream_server.start()
        except Exception as exc:
            print(f"Local EEG stream API unavailable: {exc}", file=sys.stderr)
            self.stream_server = None
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
            self.ble_worker.performance_event.connect(self.on_ble_performance_event)
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
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID.bin（单次采集单文件）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("一次采集持续写入一个 BIN；完整配置写入 manifest 和 .meta.json")
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
        self.status_label = QtWidgets.QLabel(
            "未连接。实时波形使用连续有状态滤波；原始诊断与 Alpha 分析互不影响。"
        )
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
        self.setWindowTitle(
            f"全域智能 | ADS1299 EEG 工作站 | V{APP_RELEASE_VERSION} | "
            "split BLE TX / capture pipeline"
        )
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
        log_action = file_menu.addAction("打开日志目录")
        log_action.triggered.connect(self.open_log_directory)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close, QtGui.QKeySequence.Quit)
        view_menu = self.menuBar().addMenu("视图")
        mne_action = view_menu.addAction("打开 MNE 浏览器")
        mne_action.triggered.connect(self.open_mne_browser)
        acquire_menu = self.menuBar().addMenu("采集")
        acquire_menu.addAction("连接/断开设备", self.toggle_connection)
        acquire_menu.addAction("开始采集并保存 BIN", self.start_stream)
        acquire_menu.addAction("停止采集", lambda: self.stop_stream(offer_export=True))

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
                4, 4, mark.scaled(46, 46, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
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
        self.hp_spin.setRange(0.1, 30)
        self.hp_spin.setValue(5)
        self.hp_spin.setSuffix(" Hz")
        self.lp_spin = QtWidgets.QDoubleSpinBox()
        self.lp_spin.setRange(10, 120)
        self.lp_spin.setValue(50)
        self.lp_spin.setSuffix(" Hz")
        self.notch_check = QtWidgets.QCheckBox("50/100 Hz 谐波陷波")
        self.notch_check.setChecked(True)
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
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self.stop_stream(offer_export=True))
        self.impedance_btn = QtWidgets.QPushButton("阻抗检测")
        self.impedance_btn.setToolTip("ADS1299 以 6 nA、31.25 Hz 激励并实时估算电极阻抗")
        self.impedance_btn.clicked.connect(self.open_impedance_dialog)
        self.channel_names_btn = QtWidgets.QPushButton("通道命名…")
        self.channel_names_btn.setToolTip(
            "一次设置 8 个通道名称；名称会同步到波形、PSD、导出文件并在下次启动时保留"
        )
        self.channel_names_btn.clicked.connect(self.open_channel_naming_dialog)
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
        self.reference_combo.setToolTip("V19 固定使用 SRB1：每通道信号接 INxP，公共参考接 SRB1。")
        self.reference_combo.setEnabled(False)
        self.apply_reference_btn = QtWidgets.QPushButton("应用参考")
        self.apply_reference_btn.setEnabled(False)
        self.apply_reference_btn.clicked.connect(self.apply_reference_mode)
        self.reference_combo.setEnabled(False)
        self.apply_reference_btn.setVisible(False)

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
        serial_layout.addWidget(self.channel_names_btn)
        serial_layout.addWidget(self.internal_short_btn)
        self.reference_fixed_label = QtWidgets.QLabel("参考：SRB1 固定（INxP−SRB1）")
        self.reference_fixed_label.setToolTip("GUI 已移除 SRB2 切换功能")
        serial_layout.addWidget(self.reference_fixed_label)
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
        self.differential_enabled = False
        self.differential_b_index = 1
        single_page = QtWidgets.QWidget()
        single_layout = QtWidgets.QVBoxLayout(single_page)
        single_layout.setContentsMargins(5, 5, 5, 5)
        single_header = QtWidgets.QHBoxLayout()
        self.differential_check = QtWidgets.QCheckBox("差分 A − B")
        self.differential_check.setToolTip(
            "仅改变单通道大图和 PSD 的显示信号；原始 8 通道与 BIN 保存不变"
        )
        self.differential_check.toggled.connect(self._differential_changed)
        single_header.addWidget(self.differential_check)
        single_header.addWidget(QtWidgets.QLabel("A"))
        self.single_channel_combo = QtWidgets.QComboBox()
        self.single_channel_combo.addItems([f"CH{i}" for i in range(1, CHANNELS + 1)])
        self.single_channel_combo.currentIndexChanged.connect(self._single_channel_changed)
        single_header.addWidget(self.single_channel_combo)
        single_header.addWidget(QtWidgets.QLabel("− B"))
        self.differential_b_combo = QtWidgets.QComboBox()
        self.differential_b_combo.addItems([f"CH{i}" for i in range(1, CHANNELS + 1)])
        self.differential_b_combo.setCurrentIndex(self.differential_b_index)
        self.differential_b_combo.setEnabled(False)
        self.differential_b_combo.currentIndexChanged.connect(self._differential_changed)
        single_header.addWidget(self.differential_b_combo)
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
        self.single_scale_spin.setToolTip("当前放大通道的纵轴半幅；也可在波形上滚动鼠标滚轮调整")
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
        self.single_zero_line = self.single_plot.addLine(y=0, pen=pg.mkPen("#56616b", width=1))
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
        self.single_nav_curve = self.single_nav_plot.plot(pen=pg.mkPen("#86868b", width=1))
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
        self.nav_region = pg.LinearRegionItem(
            values=(0, 10),
            movable=True,
            brush=pg.mkBrush(255, 90, 1, 45),
            pen=pg.mkPen(OMNI_ORANGE, width=1.5),
        )
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
        channel_header.setStyleSheet(
            "background:#ffffff;color:#ff5a01;border-bottom:3px solid #ff5a01;font-size:14px;font-weight:bold;"
        )
        channel_layout.addWidget(channel_header)
        self.channel_buttons = []
        self.channel_scales = []
        for ch in range(CHANNELS):
            row_widget = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 2, 0)
            row_layout.setSpacing(2)
            button = QtWidgets.QToolButton()
            button.setText(f"CH{ch + 1}")
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
            button.clicked.connect(
                lambda _checked=False, index=ch: self.open_channel_settings(index)
            )
            row_layout.addWidget(button, 1)
            self.channel_buttons.append(button)
            scale = QtWidgets.QDoubleSpinBox()
            scale.setRange(1.0, 100000.0)
            scale.setDecimals(0)
            scale.setValue(100.0)
            scale.setSuffix(" µV")
            scale.setToolTip(f"CH{ch + 1} 独立纵轴半幅；最大 100000 µV = 0.1 V")
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
            plot.setLabel("left", f"CH{ch + 1}", units="uV")
            if ch < CHANNELS - 1:
                plot.hideAxis("bottom")
            else:
                plot.setLabel("bottom", "时间", units="s")
            curve = plot.plot(pen=pg.mkPen(CHANNEL_COLORS[ch], width=2.0), connect="finite")
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
        self.bin_name = QtWidgets.QLineEdit("MMDD_HHMM_ID.bin（单次采集单文件）")
        self.bin_name.setReadOnly(True)
        self.bin_name.setToolTip("一次采集持续写入一个 BIN；完整配置写入 manifest 和 .meta.json")
        self.mode_combo = QtWidgets.QComboBox()
        for name, _, _ in MODE_ITEMS:
            self.mode_combo.addItem(name)
        self.pga_combo = QtWidgets.QComboBox()
        self.pga_combo.addItems([str(x) for x in VALID_GAINS])
        self.pga_combo.setCurrentText("24")
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItems([f"CH{i}" for i in range(1, 9)])
        self.channel_combo.currentIndexChanged.connect(self.reset_psd_smoothing)
        self.psd_raw_check = QtWidgets.QCheckBox("显示未滤波 PSD")
        self.psd_raw_check.setChecked(False)
        self.psd_raw_check.stateChanged.connect(self.reset_psd_smoothing)
        self.psd_max_spin = QtWidgets.QDoubleSpinBox()
        self.psd_max_spin.setRange(10.0, FS / 2)
        self.psd_max_spin.setValue(65.0)
        self.psd_max_spin.setSingleStep(5.0)
        self.psd_max_spin.setSuffix(" Hz")
        self.offline_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.offline_slider.valueChanged.connect(self.offline_slider_changed)
        self.offline_label = QtWidgets.QLabel()
        self.bias_checks = [QtWidgets.QCheckBox() for _ in range(8)]
        for i, cb in enumerate(self.bias_checks):
            cb.setChecked(i < 5)
        self.bias_mask_label = QtWidgets.QLabel()
        self.open_btn = QtWidgets.QPushButton()
        self.closed_btn = QtWidgets.QPushButton()
        self.time_plot = pg.PlotWidget()
        self.time_curve = self.time_plot.plot()
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
        diagnostics_log_btn = QtWidgets.QPushButton("打开日志文件夹")
        diagnostics_log_btn.setToolTip("打开异步 JSONL 操作与卡顿事件日志目录")
        diagnostics_log_btn.clicked.connect(self.open_log_folder)
        diagnostics_header.addWidget(diagnostics_log_btn)
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

        self.yrange_spin = QtWidgets.QDoubleSpinBox()
        self.yrange_spin.setValue(200)
        self.file_status = QtWidgets.QLabel("未打开文件")
        self.range_status = QtWidgets.QLabel("0.0–0.0 s")
        self.filter_status = QtWidgets.QLabel("5–50 Hz + 50/100 Hz harmonic notch")
        self.statusBar().addWidget(self.file_status, 1)
        self.statusBar().addPermanentWidget(self.range_status)
        self.statusBar().addPermanentWidget(self.filter_status)
        self.log_status = QtWidgets.QLabel("日志")
        self.log_status.setToolTip(str(APP_LOG_PATH))
        self.statusBar().addPermanentWidget(self.log_status)
        QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Left), self, activated=lambda: self.page(-1)
        )
        QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Right), self, activated=lambda: self.page(1)
        )

    def _add_tool_field(self, toolbar, label, widget, trailing=""):
        toolbar.addWidget(QtWidgets.QLabel(label + " "))
        toolbar.addWidget(widget)
        if trailing:
            toolbar.addWidget(QtWidgets.QLabel(trailing))

    def toggle_psd_dock(self, visible: bool):
        if hasattr(self, "psd_dock"):
            self.psd_dock.setVisible(bool(visible))

    def open_log_folder(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_event("open_log_folder", path=str(LOG_DIR))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(LOG_DIR)))

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
        self.win_spin.blockSignals(True)
        self.start_time_spin.blockSignals(True)
        self.win_spin.setValue(max(1, hi - lo))
        self.start_time_spin.setValue(max(0, lo))
        self.win_spin.blockSignals(False)
        self.start_time_spin.blockSignals(False)
        if self.offline_uv is not None:
            self.offline_end = min(self._total_samples(), int(hi * FS))
            self.reset_psd_smoothing()
        self.update_fast_plots()

    def page(self, direction):
        total = self._total_samples() / FS
        width = self.win_spin.value()
        self.start_time_spin.setValue(
            max(0, min(max(0, total - width), self.start_time_spin.value() + direction * width))
        )

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.MouseButtonRelease and isinstance(
            obj, QtWidgets.QAbstractButton
        ):
            text_value = str(obj.text()).replace("\n", " ").strip()
            self._last_user_action_text = text_value[:120]
            self._last_user_action_monotonic = time.monotonic()
            self.log_event(
                "gui_action",
                control=obj.objectName() or obj.__class__.__name__,
                text=text_value[:120],
                checked=bool(obj.isChecked()) if obj.isCheckable() else None,
            )
        if event.type() == QtCore.QEvent.Wheel and obj in getattr(
            self, "_scale_viewbox_channels", {}
        ):
            channel = self._scale_viewbox_channels[obj]
            if channel < 0:
                channel = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
            factor = 0.8 if event.angleDelta().y() > 0 else 1.25
            scale = self.channel_scales[channel]
            scale.setValue(float(np.clip(scale.value() * factor, scale.minimum(), scale.maximum())))
            return True
        return super().eventFilter(obj, event)

    def _channel_scale_changed(self, channel: int, value: float):
        if hasattr(self, "single_scale_spin") and int(channel) == int(self.single_channel_index):
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
        self.set_status(f"CH{channel + 1} 已在“单通道放大”Tab 中独立显示；双击其他通道可直接切换。")

    def _single_channel_changed(self, channel: int):
        if channel < 0:
            return
        self.single_channel_index = int(channel)
        self.single_scale_spin.blockSignals(True)
        self.single_scale_spin.setValue(self.channel_scales[self.single_channel_index].value())
        self.single_scale_spin.blockSignals(False)
        self._select_channel(self.single_channel_index)
        if self.differential_enabled:
            self._differential_changed()
        self.update_fast_plots()

    def _differential_changed(self, *_args):
        if not hasattr(self, "differential_check"):
            return
        self.differential_enabled = bool(self.differential_check.isChecked())
        self.differential_b_combo.setEnabled(self.differential_enabled)
        b_index = int(self.differential_b_combo.currentIndex())
        if b_index < 0:
            b_index = 1
        if self.differential_enabled and b_index == self.single_channel_index:
            b_index = (self.single_channel_index + 1) % CHANNELS
            self.differential_b_combo.blockSignals(True)
            self.differential_b_combo.setCurrentIndex(b_index)
            self.differential_b_combo.blockSignals(False)
        self.differential_b_index = b_index
        self.single_curve.setPen(
            pg.mkPen(
                OMNI_ORANGE
                if self.differential_enabled
                else CHANNEL_COLORS[self.single_channel_index],
                width=2.4,
            )
        )
        self.psd_last_signature = None
        self.reset_psd_smoothing()
        self.update_fast_plots()

    def _select_channel(self, ch):
        self.channel_combo.setCurrentIndex(ch)
        for i, curve in enumerate(self.stack_curves):
            curve.setPen(
                pg.mkPen(
                    CHANNEL_COLORS[i],
                    width=3.0 if i == ch else 2.0,
                )
            )
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

    def _main_range_changed(self, _viewbox, x_range):
        if getattr(self, "_syncing_plot", False) or self.offline_uv is None:
            return
        width = max(1.0, float(x_range[1] - x_range[0]))
        self.win_spin.blockSignals(True)
        self.start_time_spin.blockSignals(True)
        self.win_spin.setValue(width)
        self.start_time_spin.setValue(max(0.0, float(x_range[0])))
        self.win_spin.blockSignals(False)
        self.start_time_spin.blockSignals(False)
        self.offline_end = min(self._total_samples(), int(x_range[1] * FS))
        self.reset_psd_smoothing()
        self.update_fast_plots()

    def clear_stats(self):
        self.ring.clear()
        self.reset_processing_state()
        self.parser.reset()
        self.reset_live_session_metrics()
        if self.ble_worker is not None:
            self.ble_worker.reset_timing_metrics()
        self.reset_display_jitter_buffer()
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
        APP_LOGGER.info("application close requested")
        try:
            self.log_event("app_close")
            self.stop_stream()
            if self.stream_server is not None:
                self.stream_server.stop()
                self.stream_server = None
            if self.serial_worker is not None:
                self.serial_worker.stop(timeout=2.0, close_port=False)
                self.serial_worker = None
            if self.active_transport == "serial" and self.ser and self.ser.is_open:
                self.ser.close()
            if self.ble_worker is not None:
                self.ble_worker.shutdown()
            if self.filter_worker is not None:
                self.filter_worker.shutdown()
        finally:
            APP_LOGGER.info("application resources closed")
            logger = getattr(self, "event_logger", None)
            if logger is not None:
                logger.stop(timeout=2.0)
            event.accept()
