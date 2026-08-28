"""Main-window behavior grouped by responsibility."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace


class DisplayMixin:
    def _finish_offline_load(self, path: str):
        """Update navigation and plots after any supported file is loaded."""
        self.offline_end = self.offline_uv.shape[1]
        self.offline_slider.setEnabled(True)
        self.offline_slider.setRange(1, self.offline_end)
        self.offline_slider.setValue(self.offline_end)
        self.offline_label.setText(f"{Path(path).name}: {self.offline_end / FS:.1f}s")
        if hasattr(self, "file_status"):
            self.file_status.setText(
                f"{Path(path).name}  |  {FS} Hz  |  "
                f"有效采样 {int(np.sum(self.offline_valid))}/{self.offline_end}"
            )
        self.update_fast_plots()
        self.update_psd_and_info()

    def _load_bin_path(self, path: str):
        """Load one raw BIN into the shared offline/export data model."""
        raw = Path(path).read_bytes()
        parser = AdsFrameParser(self.channel_lsb_uv)
        frames = parser.feed(raw)
        if not frames:
            raise RuntimeError("没有解析出有效 48-byte 帧。")
        self.reset_processing_state()
        (
            expanded_uv,
            expanded_valid,
            expanded_seq,
            expanded_mode,
            _lost,
            _filled,
            _events,
            _large,
            _last_seq,
            _last_mode,
        ) = expand_frames_to_timeline(
            frames,
            previous_sequence=None,
            previous_mode=int(frames[0].mode),
        )
        self.offline_uv = expanded_uv.astype(np.float32, copy=False)
        self.loaded_path = str(path)
        self.offline_valid = expanded_valid
        self.offline_seq = expanded_seq
        self.offline_mode = expanded_mode
        self.current_mode = int(self.offline_mode[-1])
        if self.current_mode in (0, 1, 2):
            self.set_reference_mode_local(
                REFERENCE_SRB1 if (frames[-1].flags & 0x80) else REFERENCE_SRB2
            )
        self._finish_offline_load(path)
        return parser

    def _load_bdf_path(self, path: str):
        """Load a BDF/BDF+ file into the GUI's common 8-channel data model."""
        try:
            import mne
        except ImportError as exc:
            raise RuntimeError("读取 BDF 需要 MNE，请先安装 requirements.txt 中的依赖。") from exc

        raw = mne.io.read_raw_bdf(path, preload=True, verbose="ERROR")
        if not raw.ch_names:
            raise RuntimeError("BDF 文件中没有可读取的信号通道。")

        # Prefer physiological channels and fall back to every non-stim channel.
        picks = mne.pick_types(
            raw.info,
            eeg=True,
            eog=True,
            ecg=True,
            emg=True,
            misc=True,
            stim=False,
            exclude=[],
        )
        if not len(picks):
            picks = np.array(
                [i for i, kind in enumerate(raw.get_channel_types()) if kind != "stim"],
                dtype=int,
            )
        if not len(picks):
            raise RuntimeError("BDF 文件中没有可用的数据通道。")

        raw.pick(picks[:CHANNELS])
        source_sfreq = float(raw.info["sfreq"])
        if not np.isclose(source_sfreq, FS):
            raw.resample(FS, npad="auto", verbose="ERROR")

        data_uv = raw.get_data().astype(np.float64) * 1e6
        source_channels = data_uv.shape[0]
        if source_channels < CHANNELS:
            data_uv = np.pad(
                data_uv,
                ((0, CHANNELS - source_channels), (0, 0)),
                mode="constant",
            )
        if data_uv.shape[1] == 0:
            raise RuntimeError("BDF 文件不包含采样数据。")

        self.reset_processing_state()
        self.offline_uv = data_uv.astype(np.float32)
        self.loaded_path = str(path)
        sample_count = self.offline_uv.shape[1]
        self.offline_valid = np.ones(sample_count, dtype=bool)
        self.offline_seq = np.arange(sample_count, dtype=np.uint32)
        self.offline_mode = np.zeros(sample_count, dtype=np.uint8)
        self.current_mode = 0
        self._finish_offline_load(path)
        return source_channels, source_sfreq

    def import_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "导入文件",
            "",
            "支持的文件 (*.bin *.bdf);;ADS1299 BIN (*.bin);;BDF/BDF+ (*.bdf);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            self.stop_stream()
            if Path(path).suffix.lower() == ".bdf":
                channel_count, source_sfreq = self._load_bdf_path(path)
                resample_note = (
                    ""
                    if np.isclose(source_sfreq, FS)
                    else f"，已从 {source_sfreq:g} Hz 重采样到 {FS} Hz"
                )
                pad_note = (
                    ""
                    if channel_count == CHANNELS
                    else f"，读取 {channel_count} 个通道并补齐为 {CHANNELS} 通道"
                )
                self.set_status(
                    f"已导入 BDF：{path}{resample_note}{pad_note}，"
                    f"共 {self.offline_uv.shape[1]} 个采样点。"
                )
                return
            if Path(path).suffix.lower() != ".bin":
                raise RuntimeError("不支持该文件格式，请选择 .bin 或 .bdf 文件。")
            parser = self._load_bin_path(path)
            valid_count = int(np.sum(self.offline_valid))
            total_count = int(self.offline_uv.shape[1])
            self.set_status(
                f"已导入 {path}，有效帧 {valid_count}/{total_count}，CRC坏帧 {parser.crc_bad}。"
            )
            if (
                QtWidgets.QMessageBox.question(
                    self,
                    "转换采集文件",
                    "BIN 已导入。是否现在转换为 BDF 或 MNE FIF？",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.Yes,
                )
                == QtWidgets.QMessageBox.Yes
            ):
                self.export_biosignal_formats()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "导入失败", str(exc))

    def import_bin(self):
        """Backward-compatible entry point for older callers."""
        self.import_file()

    def offline_slider_changed(self, value: int):
        self.offline_end = int(value)
        self.reset_psd_smoothing()
        if self.offline_uv is not None:
            self.offline_label.setText(
                f"{self.offline_end / FS:.1f}/{self.offline_uv.shape[1] / FS:.1f}s"
            )
        self.update_fast_plots()

    def reset_display_jitter_buffer(self):
        self.display_cursor_sample = None
        self.display_last_tick = time.monotonic()
        self.display_target_delay_samples = int(round(DISPLAY_JITTER_BASE_TARGET_S * FS))
        self.display_startup_samples = int(round(DISPLAY_JITTER_STARTUP_S * FS))
        self.display_buffer_started = False
        self.display_buffer_state = "priming"
        self.display_buffer_underruns = 0
        self.display_low_latency_resyncs = 0
        self.display_rebuffer_events = 0
        self.display_rebuffer_started_at = None
        self.display_rebuffer_last_s = 0.0
        self.display_rebuffer_max_s = 0.0
        self.display_delay_s = 0.0
        self.display_reserve_samples = 0
        self.display_last_end_sample = -1
        self.render_gap_last_ms = 0.0
        self.render_gap_max_ms = 0.0
        self.render_gap_over_100ms = 0
        self._last_render_monotonic = None

    def update_adaptive_display_target(self):
        """Track normal BLE batching without learning one-off OS suspensions.

        The old lifetime-maximum policy permanently pinned the target near one
        second after a single multi-second Windows stall. That left only about
        170 ms between the target and the hard-resync threshold, causing a
        repeated jump/underrun cycle. A rolling p99 grows promptly and decays
        gradually, while exceptional stalls are handled once by hard resync.
        """
        if self.active_transport != "ble" or self.ble_worker is None:
            return
        try:
            timing = self.ble_worker.adaptive_timing()
        except Exception:
            return
        requested_s = min(
            DISPLAY_JITTER_MAX_TARGET_S,
            max(
                DISPLAY_JITTER_BASE_TARGET_S,
                float(timing.get("p99_s", timing.get("p95_s", 0.0))) + DISPLAY_JITTER_MARGIN_S,
                float(timing.get("recent_peak_s", 0.0)) + DISPLAY_JITTER_MARGIN_S,
            ),
        )
        requested_samples = int(round(requested_s * FS))
        previous_samples = int(self.display_target_delay_samples)
        if requested_samples > self.display_target_delay_samples:
            self.display_target_delay_samples = requested_samples
        elif requested_samples < self.display_target_delay_samples:
            shrink = max(1, int(round(DISPLAY_JITTER_TARGET_SHRINK_S_PER_TICK * FS)))
            self.display_target_delay_samples = max(
                requested_samples, self.display_target_delay_samples - shrink
            )
        if self.display_target_delay_samples > previous_samples:
            self.log_event(
                "self_repair_buffer_grow",
                level="warning",
                previous_ms=round(previous_samples * 1000.0 / FS, 3),
                target_ms=round(self.display_target_delay_samples * 1000.0 / FS, 3),
                notify_p99_ms=round(float(timing.get("p99_s", 0.0)) * 1000.0, 3),
                notify_recent_peak_ms=round(float(timing.get("recent_peak_s", 0.0)) * 1000.0, 3),
                **self.action_correlation(),
            )

    def advance_live_display_cursor(self) -> int:
        """Advance the live-only cursor behind an adaptive reserve.

        Normal playback preserves order and can temporarily run faster than real
        time to remove burst-induced lag. After an unusually long OS stall, only
        stale screen history may be skipped; raw BIN and analysis rings remain
        complete.
        """
        total = int(self.ring.total_appended)
        now = time.monotonic()
        dt = max(0.0, min(DISPLAY_JITTER_MAX_DT_S, now - self.display_last_tick))
        self.display_last_tick = now
        self.update_adaptive_display_target()
        target = max(self.display_startup_samples, self.display_target_delay_samples)

        if self.display_cursor_sample is None:
            self.display_reserve_samples = total
            self.display_delay_s = total / FS
            if total < target:
                self.display_buffer_state = "priming"
                return 0
            self.display_cursor_sample = float(max(0, total - target))
            self.display_buffer_started = True
            self.display_buffer_state = "playing"

        cursor = float(self.display_cursor_sample)
        reserve = max(0.0, float(total) - cursor)

        # Older builds entered a full "rebuffering" stop here and waited until
        # the whole reserve had refilled. For EEG that looks like an application
        # freeze. V16 continuity mode never waits for a full refill: as soon as
        # even a few new samples arrive, playback continues.
        if self.display_buffer_state == "rebuffering":
            self.display_buffer_state = "playing"
            self.display_rebuffer_started_at = None

        # Keep the screen close to real time without touching the lossless raw
        # and filtered rings. A delayed Windows notification burst can increase
        # reserve abruptly; accelerate the playback cursor smoothly to remove
        # that excess. Very stale screen history is skipped only on the display
        # path so live latency never ratchets upward for the rest of a recording.
        reserve = max(0.0, float(total) - cursor)
        hard_max = int(round(DISPLAY_JITTER_HARD_MAX_S * FS))
        hard_trigger = hard_max + int(round(DISPLAY_JITTER_HARD_HYSTERESIS_S * FS))
        if reserve > hard_trigger:
            cursor = float(max(0, total - target))
            self.display_cursor_sample = cursor
            self.display_low_latency_resyncs += 1
            self.log_event(
                "display_resync",
                level="warning",
                reserve_ms=round(reserve * 1000.0 / FS, 3),
                target_ms=round(target * 1000.0 / FS, 3),
                resync_count=int(self.display_low_latency_resyncs),
            )
            reserve = max(0.0, float(total) - cursor)

        desired_advance = dt * FS
        excess = max(0.0, reserve - float(target))
        catchup_trigger = DISPLAY_JITTER_CATCHUP_TRIGGER_S * FS
        if excess > catchup_trigger and desired_advance > 0.0:
            ramp_span = max(1.0, 0.50 * FS)
            fraction = min(1.0, (excess - catchup_trigger) / ramp_span)
            rate = 1.0 + (DISPLAY_JITTER_CATCHUP_MAX_RATE - 1.0) * fraction
            desired_advance *= rate
        elif reserve < float(target) and desired_advance > 0.0:
            # Do not freeze when Windows delivers BLE in bursts. As the reserve
            # gets low, smoothly slow visual playback instead of stopping and
            # waiting for a full buffer refill. Accepted latency remains <1 s.
            ratio = max(0.0, min(1.0, reserve / max(1.0, float(target))))
            rate = DISPLAY_JITTER_LOW_RESERVE_RATE + (1.0 - DISPLAY_JITTER_LOW_RESERVE_RATE) * ratio
            desired_advance *= rate

        max_end = float(max(0, total - self.display_min_reserve_samples))
        available_advance = max(0.0, max_end - cursor)
        was_starved = self.display_buffer_state == "starved"
        if desired_advance > 0.5 and available_advance + 1e-9 < desired_advance:
            cursor += available_advance
            self.display_cursor_sample = max(0.0, cursor)
            if not was_starved:
                self.display_buffer_underruns += 1
                self.display_rebuffer_events += 1
                self.log_event(
                    "display_underrun",
                    level="warning",
                    available_advance=round(float(available_advance), 3),
                    desired_advance=round(float(desired_advance), 3),
                    reserve_ms=round(reserve * 1000.0 / FS, 3),
                    underrun_count=int(self.display_buffer_underruns),
                )
            self.display_buffer_state = "starved"
        else:
            cursor = min(cursor + desired_advance, max_end)
            self.display_cursor_sample = max(0.0, cursor)
            self.display_buffer_state = "playing"

        end_sample = int(self.display_cursor_sample)
        self.display_reserve_samples = max(0, total - end_sample)
        self.display_delay_s = self.display_reserve_samples / FS
        return end_sample

    def get_live_window_ending_at(
        self, source: RingBuffer, n: int, end_sample: int
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return a live window ending at an absolute session sample count."""
        total = int(source.total_appended)
        end_sample = int(np.clip(end_sample, 0, total))
        lag = max(0, total - end_sample)
        request = min(source.count, max(0, int(n) + lag))
        data, valid, seq, mode = source.latest(request)
        if lag > 0:
            keep_end = max(0, data.shape[1] - lag)
            data = data[:, :keep_end]
            valid = valid[:keep_end]
            seq = seq[:keep_end]
            mode = mode[:keep_end]
        if data.shape[1] > n:
            data = data[:, -n:]
            valid = valid[-n:]
            seq = seq[-n:]
            mode = mode[-n:]
        return data, valid, seq, mode

    def get_view_data(
        self,
        seconds: float,
        filtered_live: bool = False,
        live_end_sample: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
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
        if live_end_sample is None:
            return source.latest(n)
        return self.get_live_window_ending_at(source, n, int(live_end_sample))

    def get_psd_data(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int]:
        """Return an offline interval of at least 10 s around the visible view."""
        if self.offline_uv is not None:
            total = self.offline_uv.shape[1]
            visible_start = int(round(float(self.start_time_spin.value()) * FS))
            visible_length = max(1, int(round(float(self.win_spin.value()) * FS)))
            visible_start = max(0, min(visible_start, max(0, total - 1)))
            visible_end = min(total, visible_start + visible_length)

            # Keep waveform zoom independent from spectral resolution. Center
            # a minimum ten-second PSD interval on the visible view, and shift
            # it at file boundaries to preserve its full length when possible.
            analysis_length = min(total, max(10 * FS, visible_end - visible_start))
            center = (visible_start + visible_end) // 2
            start = center - analysis_length // 2
            start = max(0, min(start, total - analysis_length))
            end = start + analysis_length
            return (
                self.offline_uv[:, start:end].copy(),
                self.offline_valid[start:end].copy(),
                self.offline_seq[start:end].copy(),
                self.offline_mode[start:end].copy(),
                start,
                end,
            )
        data, valid, seq, mode = self.get_view_data(PSD_LIVE_WINDOW_S, filtered_live=False)
        end = int(self.ring.total_appended)
        start = max(0, end - data.shape[1])
        return data, valid, seq, mode, start, end

    def prepare_plot_signal(
        self, x: np.ndarray, valid: np.ndarray, filtered_live: bool
    ) -> np.ndarray:
        x = np.asarray(x, dtype=float).copy()
        # Saturation is a quality condition, not an acquisition stop condition.
        # Filtered-live data is already channel-masked by the filter worker.
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

    def update_legacy_fast_plots(self):
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
        chain = "连续5-50Hz+50/100Hz谐波陷波" if self.filter_check.isChecked() else "原始去均值"
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
        arr = np.vstack(
            [
                self.prepare_plot_signal(stack_data[c], stack_valid, filtered_live=live_filtered)
                for c in range(CHANNELS)
            ]
        )
        std = np.nanmedian(np.nanstd(arr, axis=1))
        spacing = float(max(50.0, 5.0 * std if np.isfinite(std) else 100.0))
        for c, curve in enumerate(self.stack_curves):
            curve.setData(stack_t, arr[c] + (CHANNELS - 1 - c) * spacing)
        self.stack_plot.setXRange(float(stack_t[0]), float(stack_t[-1]), padding=0)
        self.stack_plot.setYRange(-spacing, CHANNELS * spacing, padding=0.02)
        ticks = [((CHANNELS - 1 - c) * spacing, f"CH{c + 1}") for c in range(CHANNELS)]
        self.stack_plot.getAxis("left").setTicks([ticks])

    def _observe_render_gap(self, gap_ms: float, now: float) -> None:
        if gap_ms >= 100.0 and (
            gap_ms >= 500.0 or now - self._last_render_stall_log_monotonic >= 1.0
        ):
            self._last_render_stall_log_monotonic = now
            self.log_event(
                "gui_render_stall",
                level="warning",
                gap_ms=round(float(gap_ms), 3),
                tab_index=int(self.view_tabs.currentIndex()),
                transport_pending_bytes=int(self.last_serial_waiting_bytes),
                filter_backlog_samples=int(self.filter_worker_backlog_samples()),
                display_delay_ms=round(float(self.display_delay_s) * 1000.0, 3),
                **self.action_correlation(),
            )

        ble_self_repair_enabled = bool(self.streaming and self.active_transport == "ble")
        if ble_self_repair_enabled and gap_ms >= 150.0:
            self._render_stall_times.append(now)
        elif not ble_self_repair_enabled:
            # Serial uses an independent OS reader thread and must never have
            # its PSD disabled by ordinary Windows/Qt paint jitter.
            self._render_stall_times.clear()
            self._self_repair_active = False
            self._self_repair_until = 0.0
        while self._render_stall_times and now - self._render_stall_times[0] > 10.0:
            self._render_stall_times.popleft()

        if ble_self_repair_enabled and len(self._render_stall_times) >= 3:
            self._self_repair_until = max(self._self_repair_until, now + 30.0)
            if not self._self_repair_active:
                self._self_repair_active = True
                self.log_event(
                    "self_repair_psd_pause",
                    level="warning",
                    duration_s=30.0,
                    render_stalls_10s=len(self._render_stall_times),
                )
        elif self._self_repair_active and now >= self._self_repair_until:
            self._self_repair_active = False
            self.log_event("self_repair_psd_resume")

    def update_fast_plots(self, *_args):
        """Paint from the newest USB data or BLE's delayed jitter cursor."""
        if self._plot_update_busy:
            return

        now = time.monotonic()
        if self._last_render_monotonic is not None:
            gap_ms = max(0.0, (now - self._last_render_monotonic) * 1000.0)
            self.render_gap_last_ms = gap_ms
            self.render_gap_max_ms = max(self.render_gap_max_ms, gap_ms)
            if gap_ms >= 100.0:
                self.render_gap_over_100ms += 1
            self._observe_render_gap(gap_ms, now)
        self._last_render_monotonic = now

        is_live = self.offline_uv is None
        filter_backlog_s = self.filter_worker_backlog_samples() / FS
        if is_live and self.active_transport == "serial":
            # Receive and raw recording always win. Filtering has its own FIFO;
            # Qt paints only after that worker catches up.
            self.poll_transport()
            if (
                self.sender() is getattr(self, "plot_timer", None)
                and self.packet_count == self._last_live_plot_packet
            ):
                return
            # Never intentionally freeze the waveform because a background
            # queue is busy. Transport and BIN recording are already isolated.
            # Paint the newest processed history available on every timer tick.
            _effective_lag_s = max(self.live_lag_s, filter_backlog_s)

        if is_live and self.active_transport == "ble":
            ble_backlog = self._ble_pending_bytes()
            ble_backlog_s = ble_backlog / BYTES_PER_SECOND
            _effective_lag_s = max(ble_backlog_s, filter_backlog_s)

        use_jitter = bool(is_live and self.streaming and self.active_transport == "ble")
        display_end = None
        if use_jitter:
            display_end = self.advance_live_display_cursor()
            if not self.display_buffer_started:
                if hasattr(self, "range_status"):
                    self.range_status.setText(
                        f"建立无线平滑缓冲：{self.display_reserve_samples / FS:.2f}/"
                        f"{self.display_target_delay_samples / FS:.2f} s；原始数据已正常保存"
                    )
                return
            if (
                self.sender() is getattr(self, "plot_timer", None)
                and display_end == self.display_last_end_sample
            ):
                return
            if (
                self.sender() is getattr(self, "plot_timer", None)
                and hasattr(self, "diagnostics_tab_index")
                and self.view_tabs.currentIndex() == self.diagnostics_tab_index
            ):
                # Diagnostics has no visible waveform. Keep the playback clock
                # current but do not rebuild eight hidden QPainter paths.
                self.display_last_end_sample = int(display_end or 0)
                return

        self._plot_update_busy = True
        try:
            self._render_fast_plots(display_end_sample=display_end)
            if is_live:
                self._last_live_plot_packet = self.packet_count
                if use_jitter:
                    self.display_last_end_sample = int(display_end or 0)
        except Exception as exc:
            # A transient plotting/array error must not terminate the Qt timer
            # callback chain and leave the window looking permanently frozen.
            self.plot_errors += 1
            self.log_event("plot_error", level="error", message=str(exc)[:500])
            self.set_status(f"绘图异常已隔离（采集和 BIN 保存继续）：{exc}")
        finally:
            self._plot_update_busy = False

    def _render_fast_plots(self, display_end_sample: Optional[int] = None):
        """Render eight independent channel plots with per-channel y-scales."""
        seconds = float(self.win_spin.value())
        total_n = self._total_samples()
        if total_n < 2:
            return
        total_s = total_n / FS
        if self.offline_uv is not None:
            start_s = min(float(self.start_time_spin.value()), max(0.0, total_s - seconds))
            start = int(start_s * FS)
            end = min(total_n, start + int(seconds * FS))
            data = self.offline_uv[:, start:end].copy()
            valid = self.offline_valid[start:end].copy()
            live_filtered = False
        else:
            live_filtered = bool(self.filter_check.isChecked())
            if display_end_sample is None:
                display_end_sample = int(total_n)
            display_end_sample = int(np.clip(display_end_sample, 0, total_n))
            data, valid, _seq, _mode = self.get_view_data(
                seconds,
                filtered_live=live_filtered,
                live_end_sample=display_end_sample,
            )
            start_s = max(0.0, (display_end_sample - data.shape[1]) / FS)
            self.start_time_spin.blockSignals(True)
            self.start_time_spin.setValue(start_s)
            self.start_time_spin.blockSignals(False)
        if data.shape[1] < 2:
            return
        raw_for_saturation = None
        if self.offline_uv is None and live_filtered:
            # The causal filter was already applied once when samples entered
            # filtered_ring. Never rerun zero-phase filtering on every repaint.
            arr = np.asarray(data, dtype=float)
            if valid.size == arr.shape[1] and not np.all(valid):
                arr = arr.copy()
                arr[:, ~valid] = np.nan
            raw_for_saturation, _rv, _rs, _rm = self.get_view_data(
                seconds, filtered_live=False, live_end_sample=display_end_sample
            )
        elif self.offline_uv is None:
            arr = np.asarray(data, dtype=float)
            if valid.size == arr.shape[1] and not np.all(valid):
                arr = arr.copy()
                arr[:, ~valid] = np.nan
            raw_for_saturation = np.asarray(data, dtype=float)
            with np.errstate(invalid="ignore"):
                channel_means = np.nanmean(arr, axis=1, keepdims=True)
            channel_means = np.nan_to_num(channel_means, nan=0.0, posinf=0.0, neginf=0.0)
            arr = arr - channel_means
        else:
            if self.filter_check.isChecked():
                arr = self.filter_offline_view(start, end)
            else:
                arr = np.vstack(
                    [self.prepare_plot_signal(data[c], valid, False) for c in range(CHANNELS)]
                )
            raw_for_saturation = np.asarray(data, dtype=float)

        # Mask only the screen copy. Rail-to-rail toggling otherwise creates a
        # dense vertical QPainter path like a solid rectangle and can starve the
        # GUI event loop. Raw ring/BIN and sequence counters are untouched.
        visible_saturation = np.zeros_like(arr, dtype=bool)
        if raw_for_saturation is not None and arr.ndim == 2:
            raw_for_saturation = np.asarray(raw_for_saturation, dtype=float)
            if raw_for_saturation.shape[1] >= arr.shape[1]:
                raw_for_saturation = raw_for_saturation[:, -arr.shape[1] :]
            else:
                pad = arr.shape[1] - raw_for_saturation.shape[1]
                raw_for_saturation = np.pad(
                    raw_for_saturation,
                    ((0, 0), (pad, 0)),
                    mode="constant",
                    constant_values=np.nan,
                )
            visible_saturation = self.saturation_mask_uv(raw_for_saturation)
            if self.offline_uv is None and np.any(visible_saturation):
                # Screen-only overload protection. A floating channel at the ADC
                # rails is intentionally not drawn point-to-point; doing so can
                # monopolize QPainter and starve BLE ACK scheduling. Raw ring,
                # raw BIN, timestamps and sequence numbers remain untouched.
                arr = np.asarray(arr, dtype=float).copy()
                arr[visible_saturation] = np.nan
        self.last_visible_saturated_channels = tuple(
            int(ch) for ch in np.flatnonzero(np.any(visible_saturation, axis=1))
        )
        n_plot = int(data.shape[1])
        t_rel = self._plot_time_cache.get(n_plot)
        if t_rel is None:
            t_rel = np.arange(n_plot, dtype=np.float64) / FS
            self._plot_time_cache[n_plot] = t_rel
        t = start_s + t_rel
        show_single = self.view_tabs.currentIndex() == self.single_tab_index
        single_ch = int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
        differential = bool(
            getattr(self, "differential_enabled", False) and hasattr(self, "differential_b_combo")
        )
        differential_b = int(np.clip(getattr(self, "differential_b_index", 1), 0, CHANNELS - 1))
        single_scale = float(self.channel_scales[single_ch].value())
        if show_single:
            single_signal = np.asarray(arr[single_ch], dtype=float)
            if differential:
                single_signal = single_signal - np.asarray(arr[differential_b], dtype=float)
            single_y = np.clip(single_signal, -single_scale, single_scale)
            self.single_curve.setData(t, single_y)
            self.single_plot.setXRange(start_s, start_s + seconds, padding=0)
            single_key = (single_ch, single_scale)
            if self._last_single_y_range != single_key:
                self.single_plot.setYRange(-single_scale, single_scale, padding=0)
                self._last_single_y_range = single_key
        else:
            for c, curve in enumerate(self.stack_curves):
                scale = float(self.channel_scales[c].value())
                y_plot = np.clip(np.asarray(arr[c], dtype=float), -scale, scale)
                curve.setData(t, y_plot)
                if self._last_channel_y_ranges[c] != scale:
                    self.channel_plots[c].setYRange(-scale, scale, padding=0)
                    self._last_channel_y_ranges[c] = scale
        display_chain = (
            f"{self.hp_spin.value():g}–{self.lp_spin.value():g} Hz"
            + (" + 50/100 Hz harmonic notch" if self.notch_check.isChecked() else "")
            if self.filter_check.isChecked()
            else "原始数据（去直流）"
        )
        reference = "SRB1 GLOBAL"
        signal_label = self.channel_names[single_ch]
        if differential:
            signal_label += f" − {self.channel_names[differential_b]}"
        single_title = f"{signal_label} | {display_chain} | ±{single_scale:g} uV"
        if single_title != self._last_single_title:
            self.single_plot.setTitle(single_title)
            self._last_single_title = single_title
        if differential:
            single_status = (
                f"差分 {self.channel_names[single_ch]} − "
                f"{self.channel_names[differential_b]} | 仅派生显示，原始数据不变"
            )
        else:
            single_status = (
                f"{self.channel_names[single_ch]} | "
                f"{'ON' if self.channel_enabled[single_ch] else 'OFF'}"
                f" | PGA ×{int(self.channel_gains[single_ch])}"
                f" | {'BIAS✓' if self.channel_bias[single_ch] else 'BIAS—'}"
                f" | {reference}"
            )
        if single_status != self._last_single_channel_status:
            self.single_channel_status.setText(single_status)
            self._last_single_channel_status = single_status
        if not show_single:
            self._syncing_plot = True
            # The remaining seven ViewBoxes are X-linked to the first.
            self.channel_plots[0].setXRange(start_s, start_s + seconds, padding=0)
            self._syncing_plot = False
        if self.offline_uv is not None:
            stride = max(1, total_n // 3000)
            overview = self.offline_uv[0, ::stride].astype(float)
            overview -= np.nanmean(overview)
            overview_t = np.arange(overview.size) * stride / FS
            self.nav_curve.setData(overview_t, overview)
            self.nav_plot.setXRange(0, total_s, padding=0)
            self.single_nav_curve.setData(overview_t, overview)
            self.single_nav_plot.setXRange(0, total_s, padding=0)
            self.single_nav_plot.setVisible(True)
        else:
            self.single_nav_plot.setVisible(False)
        now = time.monotonic()
        if self.offline_uv is not None or (not show_single and now - self._last_nav_update >= 0.2):
            self._last_nav_update = now
            self._syncing_nav = True
            region = (start_s, min(total_s, start_s + seconds))
            self.nav_region.setRegion(region)
            self.single_nav_region.setRegion(region)
            self._syncing_nav = False
        hp, lp = self.hp_spin.value(), self.lp_spin.value()
        notch = " + 50/100 Hz harmonic notch" if self.notch_check.isChecked() else ""
        mode = (
            f"{hp:g}–{lp:g} Hz{notch}"
            if self.filter_check.isChecked()
            else "原始数据（逐通道去直流）"
        )
        if (
            self.offline_uv is None
            and self.active_transport == "ble"
            and self.streaming
            and self.display_buffer_started
        ):
            range_text = (
                f"{start_s:.1f}–{min(total_s, start_s + seconds):.1f} s | "
                f"无线缓冲 {self.display_delay_s * 1000:.0f} ms"
            )
        else:
            range_text = f"{start_s:.1f}–{min(total_s, start_s + seconds):.1f} s"
        if self.last_visible_saturated_channels:
            channel_text = "/".join(f"CH{ch + 1}" for ch in self.last_visible_saturated_channels)
            range_text += f" | 饱和保护：{channel_text}（原始 BIN 保留）"
        if range_text != self._last_range_status_text:
            self.range_status.setText(range_text)
            self._last_range_status_text = range_text
        if mode != self._last_filter_status_text:
            self.filter_status.setText(mode)
            self._last_filter_status_text = mode

    def update_psd_and_info(self):
        """Request PSD work without blocking transport or plot painting."""
        if (
            self.active_transport == "ble"
            and self._self_repair_active
            and time.monotonic() < self._self_repair_until
        ):
            self.ble_psd_skips += 1
            self.update_info_text()
            return
        # PSD never back-pressures acquisition. It is single-flight already;
        # if the worker is busy, the current tick is simply ignored and the next
        # completed tick uses a fresh snapshot. No PSD request queue can grow.
        data, valid, seq, mode, analysis_start, analysis_end = self.get_psd_data()
        if data.shape[1] < FS * 4:
            self.latest_window_good = False
            self.psd_curve.setData([], [])
            self.latest_window_reason = "数据不足 4 秒"
            self.psd_plot.setTitle("Welch PSD | 等待至少 4 秒数据")
            self.update_info_text()
            return

        differential = bool(getattr(self, "differential_enabled", False))
        ch = (
            int(np.clip(self.single_channel_index, 0, CHANNELS - 1))
            if differential
            else self.channel_combo.currentIndex()
        )
        differential_b = int(np.clip(getattr(self, "differential_b_index", 1), 0, CHANNELS - 1))
        psd_signal = np.asarray(data[ch], dtype=float)
        saturation = self.saturation_mask_uv(data)
        channel_saturation = saturation[ch]
        self.current_psd_signal_label = self.channel_names[ch]
        if differential:
            psd_signal = psd_signal - np.asarray(data[differential_b], dtype=float)
            channel_saturation = channel_saturation | saturation[differential_b]
            self.current_psd_signal_label += f" − {self.channel_names[differential_b]}"
        # Saturation never stops PSD. It is only reported as signal quality.
        # The worker remains strictly single-flight, so expensive analysis can
        # never build a queue behind the BLE/plot path.
        signature = (
            bool(self.offline_uv is not None),
            int(analysis_start),
            int(analysis_end),
            int(ch),
            bool(differential),
            int(differential_b),
            bool(self.psd_raw_check.isChecked()),
            int(self.psd_max_spin.value()),
        )
        if self.psd_worker_busy or signature == self.psd_last_signature:
            self.update_info_text()
            return

        self.psd_last_signature = signature
        self.psd_worker_busy = True
        request_id = self.psd_request_id
        self.psd_plot.setTitle("Welch PSD | 计算中…")
        worker = PsdWorker(
            self,
            request_id,
            psd_signal,
            valid,
            seq,
            mode,
            self.sos_display_band.copy(),
            self.notch_check.isChecked(),
            live_fast=bool(self.offline_uv is None),
        )
        worker.signals.finished.connect(self.apply_psd_result)
        worker.signals.failed.connect(self.apply_psd_error)
        # Keep the QRunnable and its signal QObject alive until the queued
        # result reaches the GUI thread. Without this reference, fast live PSD
        # jobs can finish and be collected before Qt dispatches ``finished``.
        self.psd_active_worker = worker
        self.log_event(
            "psd_start",
            request_id=int(request_id),
            samples=int(psd_signal.size),
        )
        self.psd_pool.start(worker)
        self.update_info_text()

    @QtCore.Slot(int, object)
    def apply_psd_result(self, request_id: int, payload):
        self.psd_worker_busy = False
        self.psd_active_worker = None
        if request_id != self.psd_request_id:
            return

        (good, reason, metrics), x, valid, elapsed_ms = payload
        self.log_event(
            "psd_complete",
            request_id=int(request_id),
            elapsed_ms=round(float(elapsed_ms), 3),
            samples=int(np.asarray(x).size),
            good=bool(good),
        )
        self.latest_window_good = good
        self.latest_window_reason = reason
        self.latest_valid_ratio = float(np.mean(valid)) if valid.size else 0.0

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
        display_f = metrics["display_f"]
        display_p = metrics["display_p"]
        alpha_f = metrics["alpha_f"]
        alpha_p = metrics["alpha_p"]
        if raw_f.size:
            line = (raw_f >= 48) & (raw_f <= 52)
            useful = (raw_f >= 5) & (raw_f <= 45)
            lp = float(np.trapezoid(raw_p[line], raw_f[line])) if np.any(line) else np.nan
            up = float(np.trapezoid(raw_p[useful], raw_f[useful])) if np.any(useful) else np.nan
            self.latest_line_ratio = (
                lp / max(up, np.finfo(float).eps) if np.isfinite(lp) and np.isfinite(up) else np.nan
            )

        if self.psd_raw_check.isChecked():
            plot_f, plot_p = raw_f, raw_p
            plot_name = "原始诊断 PSD"
        elif alpha_f.size:
            plot_f, plot_p = alpha_f, alpha_p
            plot_name = "滤波 PSD（对数域平滑）"
        else:
            plot_f, plot_p = display_f, display_p
            plot_name = "滤波 PSD（对数域平滑）"
        if plot_f.size:
            max_hz = float(self.psd_max_spin.value())
            smoothed_db = self.smooth_psd_db(plot_f, plot_p)
            mask = (plot_f >= 1) & (plot_f <= max_hz)
            self.psd_curve.setData(plot_f[mask], smoothed_db[mask])
            self.psd_plot.setXRange(1, max_hz, padding=0)

        self.latest_alpha_power = metrics["alpha_power"]
        self.latest_alpha_peak = metrics["alpha_peak"]
        self.latest_alpha_rel = metrics["alpha_rel"]
        self.advance_alpha_capture()

        peak_text = (
            f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "---"
        )
        rel_text = (
            f"{100 * self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
        )
        self.psd_plot.setTitle(
            f"{getattr(self, 'current_psd_signal_label', '')} {plot_name} | "
            f"Alpha 峰值 {peak_text} | Alpha rate {rel_text}"
        )
        self.update_info_text()

    @QtCore.Slot(int, str)
    def apply_psd_error(self, request_id: int, message: str):
        self.psd_worker_busy = False
        self.psd_active_worker = None
        self.log_event(
            "psd_error",
            level="error",
            request_id=int(request_id),
            message=str(message)[:500],
        )
        if request_id != self.psd_request_id:
            return
        self.psd_last_signature = None
        self.latest_window_good = False
        self.latest_window_reason = f"PSD 计算失败: {message}"
        self.psd_plot.setTitle(self.latest_window_reason)
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
            QtWidgets.QMessageBox.warning(
                self, "Alpha", "SHORTED/TEST 模式只做原始诊断，不采集 Alpha。"
            )
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
        if hasattr(self, "diagnostics_pause_btn") and self.diagnostics_pause_btn.isChecked():
            return
        now_diag = time.monotonic()
        if self.streaming and (now_diag - getattr(self, "_last_diag_update_monotonic", 0.0)) < 0.50:
            return
        self._last_diag_update_monotonic = now_diag
        selected_ch = self.channel_combo.currentIndex()
        selected_config = (
            f"CH{selected_ch + 1} {'ON' if self.channel_enabled[selected_ch] else 'OFF'}, "
            f"PGA x{int(self.channel_gains[selected_ch])}, "
            f"{self.bias_register_name()}={'YES' if self.channel_bias[selected_ch] else 'NO'}, "
            + "SRB1=GLOBAL"
        )
        fs_text = f"{self.fs_est:.2f}" if np.isfinite(self.fs_est) else "---"
        alpha_peak = (
            f"{self.latest_alpha_peak:.2f} Hz" if np.isfinite(self.latest_alpha_peak) else "---"
        )
        alpha_rel = (
            f"{100 * self.latest_alpha_rel:.1f}%" if np.isfinite(self.latest_alpha_rel) else "---"
        )
        raw_rms = f"{self.latest_raw_rms:.2f} uV" if np.isfinite(self.latest_raw_rms) else "---"
        filtered_rms = (
            f"{self.latest_filtered_rms:.2f} uV" if np.isfinite(self.latest_filtered_rms) else "---"
        )
        raw_pp = f"{self.latest_raw_pp:.2f} uV" if np.isfinite(self.latest_raw_pp) else "---"
        line_ratio = (
            f"{self.latest_line_ratio:.3f}" if np.isfinite(self.latest_line_ratio) else "---"
        )
        valid_ratio = (
            f"{100 * self.latest_valid_ratio:.2f}%"
            if np.isfinite(self.latest_valid_ratio)
            else "---"
        )
        saturation = 100 * self.saturation_samples / max(1, self.packet_count * 5)
        mode = MODE_NAMES.get(self.current_mode, "UNKNOWN")
        verdict = self.make_verdict(saturation)
        raw_path = self.raw_path if self.raw_path else "---"
        quality = "PASS" if self.latest_window_good else f"REJECT ({self.latest_window_reason})"
        comparison = "---"
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            comparison = f"{10 * np.log10(max(self.closed_alpha, np.finfo(float).eps) / max(self.open_alpha, np.finfo(float).eps)):+.2f} dB"
        ble_notify_gap_last = 0.0
        ble_notify_gap_max = 0.0
        ble_notify_burst_max = 0
        ble_notify_gap_over_100ms = 0
        reliable = {
            "blocks_received": 0,
            "blocks_delivered": 0,
            "block_crc_bad": 0,
            "sync_drop": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "retransmitted_received": 0,
            "gap_markers": 0,
            "ack_sent": 0,
            "nack_sent": 0,
            "control_errors": 0,
            "pending_blocks": 0,
            "max_pending": 0,
            "expected_block": 0,
            "watchdog_nacks": 0,
            "forced_skips": 0,
            "watchdog_reconnects": 0,
            "decode_queued_bytes": 0,
            "decode_peak_bytes": 0,
            "decode_errors": 0,
            "stale_nack_suppressed": 0,
            "stale_ack_suppressed": 0,
            "session_id": None,
        }
        if self.ble_worker is not None:
            try:
                (
                    ble_notify_gap_last,
                    ble_notify_gap_max,
                    ble_notify_burst_max,
                    ble_notify_gap_over_100ms,
                ) = self.ble_worker.timing_metrics()
                reliable.update(self.ble_worker.reliable_metrics())
            except Exception:
                pass
        serial_metrics = {
            "queued_bytes": 0,
            "peak_queued_bytes": 0,
            "read_calls": 0,
            "read_errors": 0,
            "overflow_events": 0,
            "last_gap_s": 0.0,
            "max_gap_s": 0.0,
            "buffer_configured": False,
            "buffer_error": "",
        }
        if self.serial_worker is not None:
            try:
                serial_metrics.update(self.serial_worker.metrics())
            except Exception:
                pass
        adaptive_ble = {
            "profile": "---",
            "samples": 0,
            "p95_s": 0.0,
            "p99_s": 0.0,
            "recent_peak_s": 0.0,
            "ack_interval_s": 0.0,
            "nack_repeat_s": 0.0,
            "stall_reconnect_s": 0.0,
        }
        if self.ble_worker is not None:
            try:
                adaptive_ble.update(self.ble_worker.adaptive_timing())
            except Exception:
                pass
        perf_now = time.monotonic()
        if self.streaming and perf_now - self._last_periodic_perf_log_monotonic >= 10.0:
            self._last_periodic_perf_log_monotonic = perf_now
            self.log_event(
                "performance_snapshot",
                notify_gap_last_ms=round(ble_notify_gap_last * 1000.0, 3),
                notify_gap_max_ms=round(ble_notify_gap_max * 1000.0, 3),
                render_gap_last_ms=round(float(self.render_gap_last_ms), 3),
                display_delay_ms=round(float(self.display_delay_s) * 1000.0, 3),
                transport_pending_bytes=int(self.last_serial_waiting_bytes),
                reliable=dict(reliable),
                firmware_status=dict(self.ble_status),
            )
        raw_name = Path(raw_path).name if raw_path != "---" else "---"
        filter_metrics = {
            "queued_samples": 0,
            "output_samples": 0,
            "peak_queued_samples": 0,
            "batches_processed": 0,
            "errors": 0,
            "display_dropped_samples": 0,
            "last_error": "",
        }
        if self.filter_worker is not None:
            try:
                filter_metrics.update(self.filter_worker.metrics())
            except Exception:
                pass

        # V18 diagnostics: the 48-byte frame only carries the low 8 bits of the
        # firmware queue-drop counter.  A long BLE stall can therefore hide a
        # >255-frame device-side drop from the per-frame attribution logic.
        # The full 32-bit STATUS counter is authoritative for the session.
        fw_queue_drop_total = (
            int(self.ble_status.get("queue_drop", 0)) if self.active_transport == "ble" else 0
        )
        fw_missed_drdy_total = (
            int(self.ble_status.get("missed_drdy", 0)) if self.active_transport == "ble" else 0
        )
        fw_late_drdy_total = (
            int(self.ble_status.get("late_drdy", 0)) if self.active_transport == "ble" else 0
        )
        fw_acquisition_loss_total = fw_queue_drop_total + fw_missed_drdy_total + fw_late_drdy_total
        fw_acquisition_diag_available = self.active_transport != "ble" or (
            int(self.ble_status.get("status_protocol", 0)) >= 5
            and "missed_drdy" in self.ble_status
            and "late_drdy" in self.ble_status
        )
        if self.active_transport == "ble":
            gap_device_display = max(
                int(self.seq_device_lost),
                min(int(self.seq_lost), fw_acquisition_loss_total),
            )
            gap_host_display = max(0, int(self.seq_lost) - gap_device_display)
            gap_attribution_text = (
                f"{gap_device_display} / {gap_host_display}"
                if fw_acquisition_diag_available or int(self.seq_lost) == 0
                else f"未知（STATUS V{self.ble_status.get('status_protocol', '?')}）"
            )
        else:
            gap_device_display = int(self.seq_device_lost)
            gap_host_display = int(self.seq_host_lost)
            gap_attribution_text = f"{gap_device_display} / {gap_host_display}"

        entries = [
            ("Transport", self.transport_description()),
            ("Mode", mode),
            ("Streaming", str(int(self.streaming))),
            ("Channel", selected_config),
            ("Estimated Fs", f"{fs_text} Hz"),
            ("Frames", str(self.packet_count)),
            ("Sequence gaps", f"{self.seq_lost} / {self.seq_gap_events} evt"),
            ("Gap MCU/host", gap_attribution_text),
            ("CRC / sync", f"{self.parser.crc_bad} / {self.parser.sync_drop}"),
            ("Pending / queue", f"{self.last_pending} / {self.last_queue_depth}"),
            ("Queue-drop hints", str(self.queue_drop_hints)),
            ("Backlog events", str(self.backlog_events)),
            (
                "RX now / peak",
                f"{self.last_serial_waiting_bytes} / {self.transport_peak_pending_bytes} B",
            ),
            (
                "Serial worker",
                f"{serial_metrics['queued_bytes']}/{serial_metrics['peak_queued_bytes']} B",
            ),
            (
                "Serial gap/err",
                f"{1000 * serial_metrics['max_gap_s']:.0f} ms / {serial_metrics['read_errors']}",
            ),
            (
                "Serial OS buffer",
                "1MB" if serial_metrics["buffer_configured"] else "driver default",
            ),
            (
                "Turn last/max",
                f"{self.transport_last_turn_ms:.2f}/{self.transport_max_turn_ms:.2f} ms",
            ),
            ("RX lag", f"{self.live_lag_s:.3f} s"),
            ("Render gap", f"{self.render_gap_last_ms:.1f}/{self.render_gap_max_ms:.1f} ms"),
            ("Notify gap", f"{1000 * ble_notify_gap_last:.1f}/{1000 * ble_notify_gap_max:.1f} ms"),
            (
                "BLE adapt",
                f"{adaptive_ble['profile']} p95/p99/peak={1000 * adaptive_ble['p95_s']:.0f}/{1000 * adaptive_ble['p99_s']:.0f}/{1000 * adaptive_ble['recent_peak_s']:.0f}ms",
            ),
            (
                "BLE repair",
                f"N{1000 * adaptive_ble['nack_repeat_s']:.0f}/S{adaptive_ble['stall_reconnect_s']:.1f}s",
            ),
            ("Notify burst", f"{ble_notify_burst_max} B"),
            ("Display buffer", f"{self.display_delay_s:.3f} s"),
            (
                "Underrun / resync",
                f"{self.display_buffer_underruns} / {self.display_low_latency_resyncs}",
            ),
            ("Paint / PSD skip", f"{self.ble_catchup_plot_skips} / {self.ble_psd_skips}"),
            (
                "BLE MTU",
                str(
                    self.ble_status.get("mtu", self.ble_peer_mtu)
                    if self.active_transport == "ble"
                    else "---"
                ),
            ),
            (
                "FW frameQ/notifyErr",
                f"{self.ble_status.get('queue_drop', 0)} / {self.ble_status.get('notify_error', 0)}",
            ),
            (
                "FW missed/late/mutex",
                (
                    f"{self.ble_status.get('missed_drdy', 0)} / {self.ble_status.get('late_drdy', 0)} / {self.ble_status.get('mutex_busy', 0)}"
                    if fw_acquisition_diag_available
                    else "--- / --- / --- (需 STATUS V5)"
                ),
            ),
            (
                "FW bad/maxRead",
                (
                    f"{self.ble_status.get('bad_status', 0)} / {self.ble_status.get('max_read_us', 0)} us"
                    if fw_acquisition_diag_available
                    else "--- / --- (需 STATUS V5)"
                ),
            ),
            (
                "FW cmd/MTU",
                f"{self.ble_status.get('command_drop', 0)} / {self.ble_status.get('mtu_blocked', 0)}",
            ),
            ("Reliable RX", f"{reliable['blocks_received']} / {reliable['blocks_delivered']}"),
            ("Reliable pending", f"{reliable['pending_blocks']} / max {reliable['max_pending']}"),
            (
                "BLE decode Q/peak",
                f"{reliable['decode_queued_bytes']} / {reliable['decode_peak_bytes']} B",
            ),
            ("BLE decode errors", str(reliable["decode_errors"])),
            ("Retrans / gap", f"{reliable['retransmitted_received']} / {reliable['gap_markers']}"),
            ("ACK / NACK", f"{reliable['ack_sent']} / {reliable['nack_sent']}"),
            (
                "Watchdog N/S/R",
                f"{reliable['watchdog_nacks']}/{reliable['forced_skips']}/{reliable['watchdog_reconnects']}",
            ),
            ("Dup / OOO", f"{reliable['duplicates']} / {reliable['out_of_order']}"),
            ("Reliable CRC/sync", f"{reliable['block_crc_bad']} / {reliable['sync_drop']}"),
            ("Control errors", str(reliable["control_errors"])),
            (
                "Stale ctrl suppressed",
                f"N{reliable['stale_nack_suppressed']} / A{reliable['stale_ack_suppressed']}",
            ),
            (
                "FW retained/flight",
                f"{self.ble_status.get('reliable_stored', 0)} / {self.ble_status.get('reliable_outstanding', 0)}",
            ),
            (
                "FW ACK / NACK",
                f"{self.ble_status.get('reliable_ack_count', 0)} / {self.ble_status.get('reliable_nack_count', 0)}",
            ),
            (
                "FW retrans/recover",
                f"{self.ble_status.get('reliable_retransmit', 0)} / {self.ble_status.get('reliable_recovered', 0)}",
            ),
            (
                "FW overflow/unknown",
                f"{self.ble_status.get('reliable_overflow', 0)} / {self.ble_status.get('reliable_unknown_nack', 0)}",
            ),
            (
                "FW recent o/u/p",
                f"{self.ble_status_delta.get('reliable_overflow', 0)} / {self.ble_status_delta.get('reliable_unknown_nack', 0)} / {self.ble_status_delta.get('reliable_protocol_error', 0)}",
            ),
            (
                "Filter queue/out",
                f"{filter_metrics['queued_samples']} / {filter_metrics['output_samples']} smp",
            ),
            (
                "Filter peak/batch",
                f"{filter_metrics['peak_queued_samples']} / {filter_metrics['batches_processed']}",
            ),
            (
                "Filter error/drop",
                f"{filter_metrics['errors']} / {filter_metrics['display_dropped_samples']}",
            ),
            ("Raw queue", f"{self.raw_writer.queued_bytes} B"),
            ("Write / plot err", f"{self.raw_write_errors} / {self.plot_errors}"),
            ("Raw file", raw_name),
            ("Event log", Path(self.event_logger.path).name),
            (
                "Log drop/error",
                f"{self.event_logger.dropped} / {self.event_logger.error or 'none'}",
            ),
            ("Self repair", "PSD paused" if self._self_repair_active else "monitoring"),
            ("Raw / filt RMS", f"{raw_rms} / {filtered_rms}"),
            ("Peak-to-peak", raw_pp),
            ("Valid samples", valid_ratio),
            ("50Hz ratio", line_ratio),
            ("Alpha peak/rel", f"{alpha_peak} / {alpha_rel}"),
            ("Saturation", f"{saturation:.4f}%"),
            ("Signal quality", quality),
            ("BIAS mask", f"0x{self.current_bias_mask():02X}"),
            ("Alpha C/O", comparison),
        ]

        def compact_item(item):
            label, value = item
            value = str(value).replace("\n", " ")
            if len(value) > 29:
                value = value[:28] + "…"
            return f"{label:<19}: {value:<29}"

        columns = 3
        rows = (len(entries) + columns - 1) // columns
        lines = []
        for row in range(rows):
            cells = []
            for col in range(columns):
                idx = row + col * rows
                cells.append(compact_item(entries[idx]) if idx < len(entries) else " " * 51)
            lines.append(" | ".join(cells).rstrip())
        lines.append("-" * 118)
        lines.append(f"判断: {verdict}")
        text = "\n".join(lines)

        bar = self.info_text.verticalScrollBar()
        previous_value = bar.value()
        was_at_bottom = previous_value >= max(0, bar.maximum() - 2)
        if text != self.info_text.toPlainText():
            self.info_text.setPlainText(text)
            bar = self.info_text.verticalScrollBar()
            if was_at_bottom:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(previous_value, bar.maximum()))

    def make_verdict(self, saturation: float) -> str:
        if self.packet_count < FS * 2 and self.offline_uv is None:
            return "数据不足，至少采集 2 秒。"
        if self.parser.crc_bad > 0:
            return "有 CRC 错：先查传输缓存、帧格式以及 USB/BLE 链路。"
        if self.status_bad > 0:
            return "ADS STATUS 异常：怀疑 SPI 位/字节错位。"

        reliable = self.ble_worker.reliable_metrics() if self.ble_worker is not None else {}
        if self.active_transport == "ble":
            fw_queue_drop_total = int(self.ble_status.get("queue_drop", 0))
            fw_missed_drdy_total = int(self.ble_status.get("missed_drdy", 0))
            fw_late_drdy_total = int(self.ble_status.get("late_drdy", 0))
            fw_mutex_busy_total = int(self.ble_status.get("mutex_busy", 0))
            fw_acquisition_loss_total = (
                fw_queue_drop_total + fw_missed_drdy_total + fw_late_drdy_total
            )
            fw_acquisition_diag_available = (
                int(self.ble_status.get("status_protocol", 0)) >= 5
                and "missed_drdy" in self.ble_status
                and "late_drdy" in self.ble_status
            )
            effective_device_gap = max(
                int(self.seq_device_lost),
                min(int(self.seq_lost), fw_acquisition_loss_total),
            )
            effective_host_gap = max(0, int(self.seq_lost) - effective_device_gap)

            overflow_total = int(self.ble_status.get("reliable_overflow", 0))
            unknown_total = int(self.ble_status.get("reliable_unknown_nack", 0))
            protocol_total = int(self.ble_status.get("reliable_protocol_error", 0))
            overflow_recent = int(self.ble_status_delta.get("reliable_overflow", 0))
            unknown_recent = int(self.ble_status_delta.get("reliable_unknown_nack", 0))
            protocol_recent = int(self.ble_status_delta.get("reliable_protocol_error", 0))

            if overflow_recent > 0:
                return (
                    "BLE 固件可靠保留环刚发生新增溢出；采集仍会继续，但这段可能有真实 EEG 缺口。"
                    "重点看 Notify gap、FW retained/flight 和 frameQueue drop，而不是 GUI 绘图。"
                )
            if int(reliable.get("forced_skips", 0)) > 0:
                return (
                    "BLE 曾有无法及时补回的 block；V18 会保留真实缺口并继续后续采集。"
                    "不会为了等待单个 block 把整条时域链长期堵住。"
                )
            if protocol_recent > 0:
                return (
                    "BLE 控制包刚出现格式/CRC/类型异常；V18 已把过期 ACK/NACK 从协议错误中分离并在发送前抑制。"
                    "若 recent p 持续增长，再检查 GUI/固件是否确为同一 V18 包。"
                )
            if unknown_recent > 0:
                return (
                    "BLE 刚有重传请求未命中保留块；V18 固件会忽略已 ACK 的过期 NACK，"
                    "只有真正仍未确认且已不在保留环的请求才计 unknown。继续看 overflow/gap 是否同步增长。"
                )
            # Historical counters are intentionally not a permanent error. In
            # V17 a stale NACK race could increment unknown once and make the
            # verdict stay red for the rest of an overnight capture.
            if (overflow_total or unknown_total or protocol_total) and (
                overflow_recent == 0 and unknown_recent == 0 and protocol_recent == 0
            ):
                pass
            if fw_queue_drop_total > 0:
                return (
                    f"MCU frameQueue 已实际丢 {fw_queue_drop_total} 帧；这不是 GUI/Serial 误报。"
                    "若同时看到 Notify gap 很大或 notifyErr 增长，说明 BLE 栈暂停曾拖住旧版传输任务；"
                    "V18 已将 frameQueue→可靠环 与 BLE notify 拆成独立任务。"
                )
            if fw_missed_drdy_total > 0 or fw_late_drdy_total > 0:
                return (
                    "MCU 采集侧已有缺口："
                    f"missedDRDY={fw_missed_drdy_total}, "
                    f"lateRead={fw_late_drdy_total}, mutexBusy={fw_mutex_busy_total}。"
                    "这不是 Python/BLE 排序丢帧；请检查 DRDY 实时性、SPI 读取与 ADS 锁占用。"
                )
            if (
                int(self.ble_status.get("notify_error", 0))
                or int(self.ble_status.get("command_drop", 0))
                or int(self.ble_status.get("mtu_blocked", 0))
            ):
                return (
                    "BLE Notify/控制/MTU 仍有异常计数，但 MCU frameQueue 尚未丢帧。"
                    "继续看 Notify gap、Reliable retained/flight 与 retrans/recover。"
                )
            if effective_device_gap > 0 or self.backlog_events > 0 or self.queue_drop_hints > 0:
                return "MCU/DRDY 或固件采集链确有缺口：优先检查 SPI 实时性和 MCU frameQueue。"
            if effective_host_gap > 0:
                if not fw_acquisition_diag_available:
                    return (
                        "BLE ADS 序号有缺口，但当前固件 STATUS 不含 DRDY/读取计数，"
                        "无法判定缺口在 MCU 采集侧还是主机侧；请烧录配套 SRB1 STATUS V5 固件。"
                    )
                return (
                    "BLE ADS 序号仍有主机侧缺口，但 MCU 未报告采集队列丢帧；"
                    "优先看 reliable pending/重传/decode queue。"
                )
            if self.ble_worker is not None:
                try:
                    _last_gap, max_gap, _burst, _long_count = self.ble_worker.timing_metrics()
                except Exception:
                    max_gap = 0.0
                if max_gap >= 1.0:
                    return (
                        f"BLE/Windows 曾暂停交付 {max_gap:.2f} s；原始可靠数据可继续恢复，"
                        "显示层会执行一次低延迟重同步。"
                    )
        else:
            if self.seq_device_lost > 0 or self.backlog_events > 0 or self.queue_drop_hints > 0:
                return (
                    "MCU/DRDY 或固件队列确有缺口：优先检查 SPI 实时性、任务积压和固件 queue drop。"
                )
            if self.seq_host_lost > 0:
                return "USB 序号有缺口但 MCU 未报告 pending/queue drop：检查 Serial worker 与 OS 串口缓存。"

        if np.isfinite(self.fs_est) and abs(self.fs_est - FS) > 2:
            return f"采样率 {self.fs_est:.1f} Hz 偏离 250 Hz。"
        if saturation > 0.1:
            return "有样本接近满量程：V18 会继续采集/保存，并只隔离该通道的实时绘图负担；仍需检查参考、BIAS 或电极。"
        if np.isfinite(self.latest_line_ratio) and self.latest_line_ratio > 0.25:
            return "原始 50 Hz 占比高：陷波后看着干净也不代表硬件健康。"
        if not self.latest_window_good:
            return f"当前 Alpha 窗被拒绝：{self.latest_window_reason}。"
        if np.isfinite(self.open_alpha) and np.isfinite(self.closed_alpha):
            delta = 10 * np.log10(
                max(self.closed_alpha, np.finfo(float).eps)
                / max(self.open_alpha, np.finfo(float).eps)
            )
            return f"20秒中位数：闭眼/睁眼 Alpha = {delta:+.2f} dB。"
        return "数字链路与当前分析窗正常；可分别采集20秒睁眼和闭眼。"
