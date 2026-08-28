"""Main-window behavior grouped by responsibility."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace


class AcquisitionMixin:
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
        self.filter_generation = int(getattr(self, "filter_generation", 0)) + 1
        worker = getattr(self, "filter_worker", None)
        if worker is not None:
            use_notch = self.notch_check.isChecked() if hasattr(self, "notch_check") else True
            worker.configure(
                self.filter_generation,
                self.sos_display_band,
                self.sos_notch,
                use_notch,
            )
        self.reset_psd_smoothing()

    def reset_psd_smoothing(self, *_args):
        self.psd_smooth_f: Optional[np.ndarray] = None
        self.psd_smooth_db: Optional[np.ndarray] = None
        self.psd_request_id += 1
        self.psd_last_signature = None

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

    def clean_with_valid(
        self, x: np.ndarray, valid: Optional[np.ndarray], max_gap: int = 2
    ) -> Tuple[np.ndarray, bool, int]:
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
            if not hasattr(self, "notch_check") or self.notch_check.isChecked():
                y = signal.sosfiltfilt(self.sos_notch, y)
        except ValueError:
            y = signal.sosfilt(self.sos_display_band, x)
            if not hasattr(self, "notch_check") or self.notch_check.isChecked():
                y = signal.sosfilt(self.sos_notch, y)
        return y

    def filter_offline_view(self, start: int, end: int) -> np.ndarray:
        """Filter an offline view with context, then crop it to the visible range.

        Filtering only the visible samples makes ``sosfiltfilt`` treat both
        screen edges as signal boundaries.  Its startup/ending transient then
        looks like a real amplitude wobble.  Extra samples (or reflected data
        at the actual file boundaries) keep that transient outside the view.
        """
        view_len = max(0, end - start)
        if self.offline_uv is None or view_len == 0:
            return np.empty((CHANNELS, 0), dtype=float)

        hp = max(0.1, float(self.hp_spin.value()))
        context = max(3 * FS, int(np.ceil(5.0 * FS / hp)))
        source_len = self.offline_uv.shape[1]
        context_start = max(0, start - context)
        context_end = min(source_len, end + context)
        values = self.offline_uv[:, context_start:context_end].astype(float, copy=True)
        valid = self.offline_valid[context_start:context_end].copy()

        left_missing = max(0, context - (start - context_start))
        right_missing = max(0, context - (context_end - end))
        if left_missing or right_missing:
            # Reflecting the real endpoint gives the zero-phase filter enough
            # settling data without inventing a step or changing saved data.
            pad_mode = "reflect" if values.shape[1] > 1 else "edge"
            values = np.pad(values, ((0, 0), (left_missing, right_missing)), mode=pad_mode)
            valid = np.pad(valid, (left_missing, right_missing), mode="edge")

        crop_start = left_missing + (start - context_start)
        crop_end = crop_start + view_len
        filtered = np.vstack(
            [self.filter_offline_display(values[ch], valid) for ch in range(CHANNELS)]
        )
        result = filtered[:, crop_start:crop_end]
        view_valid = self.offline_valid[start:end]
        if view_valid.size == result.shape[1] and not np.all(view_valid):
            result[:, ~view_valid] = np.nan
        return result

    def filter_for_psd(
        self,
        x: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> np.ndarray:
        """Apply the toolbar's current band-pass/notch settings for PSD."""
        x = signal.detrend(np.asarray(x, dtype=float), type="linear")
        if x.size < 64:
            return x
        band = self.sos_display_band if sos_band is None else np.asarray(sos_band)
        notch_enabled = (
            self.notch_check.isChecked()
            if use_notch is None and hasattr(self, "notch_check")
            else bool(use_notch)
        )
        try:
            y = signal.sosfiltfilt(band, x)
            if notch_enabled:
                y = signal.sosfiltfilt(self.sos_notch, y)
        except ValueError:
            y = signal.sosfilt(band, x)
            if notch_enabled:
                y = signal.sosfilt(self.sos_notch, y)
        return y

    def append_live_filtered(
        self,
        frames: List[Frame],
        values: Optional[np.ndarray] = None,
        valid: Optional[np.ndarray] = None,
        sequence: Optional[np.ndarray] = None,
        modes: Optional[np.ndarray] = None,
    ):
        if not frames:
            return
        if values is None:
            values = np.stack([fr.uv for fr in frames], axis=1).astype(np.float32)
        if valid is None:
            valid = np.array([fr.valid for fr in frames], dtype=bool)
        if sequence is None:
            sequence = np.array([fr.sequence for fr in frames], dtype=np.uint32)
        if modes is None:
            modes = np.array([fr.mode for fr in frames], dtype=np.uint8)
        source = np.asarray(values, dtype=float)
        channel_good_matrix = np.asarray(valid, dtype=bool)[None, :] & np.isfinite(source)
        filled = source.copy()
        # Missing/saturated samples are forward-filled only for evolving the
        # causal filter state. The corresponding channel output is restored to
        # NaN below, so no EEG value is invented.
        for ch in range(CHANNELS):
            channel_good = channel_good_matrix[ch]
            if not self.have_filter_input[ch]:
                first_candidates = np.flatnonzero(channel_good)
                if first_candidates.size:
                    first_idx = int(first_candidates[0])
                    first_value = float(filled[ch, first_idx])
                    filled[ch, :first_idx] = first_value
                    self.display_zi_band[ch] = (
                        signal.sosfilt_zi(self.sos_display_band) * first_value
                    )
                    self.display_zi_notch[ch].fill(0.0)
                    self.last_filter_input[ch] = first_value
                    self.have_filter_input[ch] = True
            if channel_good.all():
                if filled.shape[1]:
                    self.last_filter_input[ch] = float(filled[ch, -1])
                    self.have_filter_input[ch] = True
            else:
                n_samples = int(filled.shape[1])
                if n_samples:
                    original = filled[ch].copy()
                    seed = float(self.last_filter_input[ch]) if self.have_filter_input[ch] else 0.0
                    last_good_index = np.where(
                        channel_good, np.arange(n_samples, dtype=np.int64), -1
                    )
                    np.maximum.accumulate(last_good_index, out=last_good_index)
                    has_previous = last_good_index >= 0
                    filled[ch, ~has_previous] = seed
                    if np.any(has_previous):
                        filled[ch, has_previous] = original[last_good_index[has_previous]]
                    good_indices = np.flatnonzero(channel_good)
                    if good_indices.size:
                        self.last_filter_input[ch] = float(original[int(good_indices[-1])])
                        self.have_filter_input[ch] = True

        # SciPy accepts all eight channels in one call when the sample axis is 1.
        # State is transposed from (channel, section, 2) to the required
        # (section, channel, 2), cutting the hot path from 16 sosfilt calls per
        # BLE batch to only two calls.
        band_zi = np.transpose(self.display_zi_band, (1, 0, 2))
        filtered, band_zf = signal.sosfilt(self.sos_display_band, filled, axis=1, zi=band_zi)
        self.display_zi_band = np.transpose(band_zf, (1, 0, 2))
        if not hasattr(self, "notch_check") or self.notch_check.isChecked():
            notch_zi = np.transpose(self.display_zi_notch, (1, 0, 2))
            filtered, notch_zf = signal.sosfilt(self.sos_notch, filtered, axis=1, zi=notch_zi)
            self.display_zi_notch = np.transpose(notch_zf, (1, 0, 2))

        # One non-finite value must not poison an IIR state forever and make all
        # later waveforms disappear.  Reset only the affected channel state and
        # pass a finite fallback through this display batch; validity masks still
        # preserve real packet gaps.
        bad_channels = np.flatnonzero(
            ~np.all(np.isfinite(filtered), axis=1)
            | ~np.all(np.isfinite(self.display_zi_band), axis=(1, 2))
            | ~np.all(np.isfinite(self.display_zi_notch), axis=(1, 2))
        )
        for ch in bad_channels:
            seed = (
                float(self.last_filter_input[ch])
                if np.isfinite(self.last_filter_input[ch])
                else 0.0
            )
            self.display_zi_band[ch] = signal.sosfilt_zi(self.sos_display_band) * seed
            self.display_zi_notch[ch].fill(0.0)
            filtered[ch] = np.nan_to_num(filtered[ch], nan=seed, posinf=seed, neginf=seed)
        filtered[~channel_good_matrix] = np.nan
        self.filtered_ring.append_batch(
            np.asarray(filtered, dtype=np.float32), valid, sequence, modes
        )

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
            return False, f"有效样本仅 {100 * valid_ratio:.2f}%", cleaned
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

    def compute_live_psd_fast(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> Tuple[bool, str, dict]:
        """Cheap, saturation-tolerant PSD for the live path.

        Exactly one raw Welch and one filtered Welch are evaluated. The older
        quality-window loop could execute many additional Welch transforms per
        refresh; that is useful offline but unnecessary for a live monitor.
        Saturated samples remain finite and are intentionally analysed instead
        of pausing PSD or generating extra jobs.
        """
        x = np.asarray(x, dtype=float)
        valid = np.asarray(valid, dtype=bool)
        cleaned, _gap_ok, max_gap = self.clean_with_valid(x, valid, max_gap=2)
        metrics = {
            "cleaned": cleaned,
            "raw_f": np.array([]),
            "raw_p": np.array([]),
            "display_f": np.array([]),
            "display_p": np.array([]),
            "alpha_f": np.array([]),
            "alpha_p": np.array([]),
            "alpha_power": np.nan,
            "alpha_peak": np.nan,
            "alpha_rel": np.nan,
            "filtered_rms": np.nan,
            "good_segments": 0,
            "total_segments": 1,
        }
        if cleaned.size < FS * 4:
            return False, "不足 4 秒", metrics
        nperseg = min(cleaned.size, FS * 4)
        nfft = 1024
        noverlap = min(nperseg // 2, nperseg - 1)
        raw_f, raw_p = signal.welch(
            cleaned,
            fs=FS,
            window="hann",
            nperseg=nperseg,
            noverlap=noverlap,
            nfft=nfft,
            detrend=False,
        )
        display_signal = self.filter_for_psd(cleaned, sos_band=sos_band, use_notch=use_notch)
        display_f, display_p = signal.welch(
            display_signal,
            fs=FS,
            window="hann",
            nperseg=nperseg,
            noverlap=noverlap,
            nfft=nfft,
        )
        metrics["raw_f"] = raw_f
        metrics["raw_p"] = raw_p
        metrics["display_f"] = display_f
        metrics["display_p"] = display_p
        metrics["alpha_f"] = display_f
        metrics["alpha_p"] = display_p
        metrics["filtered_rms"] = float(np.sqrt(np.mean(display_signal**2)))
        alpha = (display_f >= 8) & (display_f <= 13)
        broad = (display_f >= 4) & (display_f <= 30)
        if np.any(alpha) and np.any(broad):
            alpha_power = float(np.trapezoid(display_p[alpha], display_f[alpha]))
            broad_power = float(np.trapezoid(display_p[broad], display_f[broad]))
            af = display_f[alpha]
            metrics["alpha_power"] = alpha_power
            metrics["alpha_peak"] = float(af[int(np.argmax(display_p[alpha]))])
            metrics["alpha_rel"] = alpha_power / max(broad_power, np.finfo(float).eps)
        sat = self.saturation_mask_uv(cleaned[None, :])[0]
        sat_ratio = float(np.mean(sat)) if sat.size else 0.0
        valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        metrics["good_segments"] = 1
        if sat_ratio > 0.0:
            reason = f"PSD持续计算；饱和样本 {sat_ratio * 100:.1f}%"
            return False, reason, metrics
        if valid_ratio < 0.99 or max_gap > 2:
            return False, f"PSD持续计算；有效样本 {valid_ratio * 100:.1f}%", metrics
        return True, "实时 PSD 正常", metrics

    def compute_alpha_from_window(
        self,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
        sos_band: Optional[np.ndarray] = None,
        use_notch: Optional[bool] = None,
    ) -> Tuple[bool, str, dict]:
        x = np.asarray(x, dtype=float)
        valid = np.asarray(valid, dtype=bool)
        self.latest_valid_ratio = float(np.mean(valid)) if valid.size else 0.0
        cleaned_all, _gap_ok, _max_gap = self.clean_with_valid(x, valid, max_gap=2)
        metrics = {
            "cleaned": cleaned_all,
            "raw_f": np.array([]),
            "raw_p": np.array([]),
            "display_f": np.array([]),
            "display_p": np.array([]),
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

        # Raw diagnostic PSD uses the input samples without band-pass, notch,
        # or detrending. Welch still applies its analysis window and averaging.
        nfft = max(2048, 2 ** int(np.ceil(np.log2(segment_len))))
        raw_f, raw_p = signal.welch(
            cleaned_all,
            fs=FS,
            window="hann",
            nperseg=segment_len,
            noverlap=3 * segment_len // 4,
            nfft=nfft,
            detrend=False,
        )
        metrics["raw_f"] = raw_f
        metrics["raw_p"] = raw_p

        # Prepare the default PSD with the toolbar's current filter settings.
        # Alpha quality
        # windows may all be rejected (for example during movement); that
        # should change the quality verdict, not leave the spectrum blank.
        display_signal = self.filter_for_psd(cleaned_all, sos_band=sos_band, use_notch=use_notch)
        display_f, display_p = signal.welch(
            display_signal,
            fs=FS,
            window="hann",
            nperseg=segment_len,
            noverlap=3 * segment_len // 4,
            nfft=nfft,
        )
        metrics["display_f"] = display_f
        metrics["display_p"] = display_p
        metrics["filtered_rms"] = float(np.sqrt(np.mean(display_signal**2)))

        # Always expose Alpha peak/rate from the current Alpha analysis chain.
        # Window-quality screening is still retained for the optional 20-second
        # capture workflow, but it no longer blanks the PSD's basic metrics.
        display_alpha = (display_f >= 8) & (display_f <= 13)
        display_broad = (display_f >= 4) & (display_f <= 30)
        if np.any(display_alpha) and np.any(display_broad):
            display_alpha_power = float(
                np.trapezoid(display_p[display_alpha], display_f[display_alpha])
            )
            display_broad_power = float(
                np.trapezoid(display_p[display_broad], display_f[display_broad])
            )
            display_af = display_f[display_alpha]
            metrics["alpha_power"] = display_alpha_power
            metrics["alpha_peak"] = float(display_af[int(np.argmax(display_p[display_alpha]))])
            metrics["alpha_rel"] = display_alpha_power / max(
                display_broad_power, np.finfo(float).eps
            )

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
            alpha_signal = self.filter_for_psd(segment_x, sos_band=sos_band, use_notch=use_notch)
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
            common = (
                max(set(reject_reasons), key=reject_reasons.count)
                if reject_reasons
                else "无合格片段"
            )
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
            if self.active_transport == "ble":
                self.impedance_active = True
                self.impedance_mask = mask
                ack = self._ble_write_bulk_config(REFERENCE_SRB1)
                snapshot = self.ble_worker.config_snapshot
                expected_p = mask
                expected_n = 0
                confirmed = (
                    snapshot is not None
                    and snapshot.verified
                    and snapshot.lead_off_p == expected_p
                    and snapshot.lead_off_n == expected_n
                )
            else:
                self.transport_write(bytes((0xA9, mask & 0xFF)))
                ack = self.read_config_ack(0xA9, expected_argument=mask & 0xFF)
                expected_p = mask
                expected_n = 0
                confirmed = (
                    ack is not None
                    and ack["verified"]
                    and ack["loff_p"] == expected_p
                    and ack["loff_n"] == expected_n
                    and ack["loff_config"] == 0x02
                )
            if not confirmed:
                raise RuntimeError("固件未确认 LOFF 寄存器。请烧录本版本配套固件后重试。")
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
                    if self.active_transport == "ble":
                        self.impedance_active = False
                        self.impedance_mask = 0
                        self._ble_write_bulk_config(REFERENCE_SRB1)
                    else:
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
                if self.active_transport == "ble":
                    self.impedance_active = False
                    self.impedance_mask = 0
                    self._ble_write_bulk_config(REFERENCE_SRB1)
                    snapshot = self.ble_worker.config_snapshot
                    if snapshot is None or snapshot.lead_off_p != 0 or snapshot.lead_off_n != 0:
                        error = "ADS1299 未确认 LOFF 已关闭"
                else:
                    self.transport_write(bytes((0xA9, 0x00)))
                    ack = self.read_config_ack(0xA9, expected_argument=0)
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
        carrier = np.column_stack(
            (
                np.sin(2.0 * np.pi * LEAD_OFF_FREQUENCY_HZ * t),
                np.cos(2.0 * np.pi * LEAD_OFF_FREQUENCY_HZ * t),
                np.ones_like(t),
            )
        )
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
            impedance_kohm = max(0.0, carrier_peak_uv / LEAD_OFF_CURRENT_NA - series_kohm)
            value_label.setText(">999 kΩ" if impedance_kohm > 999.0 else f"{impedance_kohm:.1f} kΩ")
            if impedance_kohm < 10.0:
                text, color = "良好", "#258b3b"
            elif impedance_kohm <= 50.0:
                text, color = "可用", "#d97800"
            else:
                text, color = "接触不良", "#c62828"
            quality_label.setText(text)
            quality_label.setStyleSheet(f"color:{color};font-weight:700;")

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
        # A pause inferred from a previous BLE/idle paint stall must not leak
        # into a new recording, especially a direct serial session.
        self._render_stall_times.clear()
        self._self_repair_active = False
        self._self_repair_until = 0.0
        self.session_started_monotonic = time.monotonic()
        # Clear the locally displayed firmware counters immediately. The 'r'
        # command below resets the matching counters on the C3 itself.
        for key in (
            "queue_drop",
            "notify_error",
            "command_drop",
            "mtu_blocked",
            "blocks_sent",
            "reliable_stored",
            "reliable_outstanding",
            "reliable_ack_count",
            "reliable_nack_count",
            "reliable_retransmit",
            "reliable_recovered",
            "reliable_overflow",
            "reliable_unknown_nack",
            "reliable_protocol_error",
            "missed_drdy",
            "late_drdy",
            "mutex_busy",
            "bad_status",
            "max_read_us",
        ):
            if key in self.ble_status:
                self.ble_status[key] = 0
        self.ble_status_delta = {}

    def start_stream(self):
        if self.streaming:
            self.log_event(
                "recording_start_ignored",
                reason="already_streaming",
                recording_id=self.recording_session_id,
                bin_file=self.raw_path,
            )
            self.set_status(f"已在采集：ID {self.recording_session_id}，忽略重复开始")
            return
        if not self.require_transport():
            return
        if self.active_transport == "ble" and int(
            self.ble_status.get("status_protocol", 0)
        ) not in (0x03, 0x04, 0x05):
            QtWidgets.QMessageBox.warning(
                self,
                "BLE 固件不匹配",
                "未收到受支持的 BLE STATUS V3/V4/V5。请确认连接的是 OmniBCI SRB1 设备，"
                "并优先烧录本项目配套的 STATUS V5 固件。",
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
            self.log_event(
                "recording_start",
                recording_id=self.recording_session_id,
                bin_file=self.raw_path,
                manifest_file=self.recording_manifest_path,
            )
            if self.stream_server is not None:
                self.stream_server.begin_recording(
                    self.recording_session_id,
                    started_at=time.time(),
                )
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
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.set_status(
                f"实时采集中：ID {self.recording_session_id}，连续 BIN：{Path(self.raw_path).name}"
            )
        except Exception as exc:
            if self.stream_server is not None:
                self.stream_server.end_recording()
            self.close_raw_file()
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
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
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.ble_worker is not None:
            self.ble_worker.set_streaming_hint(False)
        self.close_raw_file()
        if self.stream_server is not None:
            self.stream_server.end_recording()
        snap = self.raw_writer.snapshot()
        if was_streaming:
            self.log_event(
                "recording_stop",
                recording_id=self.recording_session_id,
                total_bytes=int(snap.get("bytes_written", 0)),
                bin_file=str(snap.get("first_path", first_path or "")),
            )
        if was_streaming and self.recording_session_id:
            manifest_name = (
                Path(self.recording_manifest_path).name if self.recording_manifest_path else "---"
            )
            self.set_status(
                f"采集已停止：ID {self.recording_session_id}，连续 BIN 已封口；清单 {manifest_name}"
            )
        if (
            offer_export
            and was_streaming
            and first_path
            and Path(first_path).exists()
            and Path(first_path).stat().st_size
        ):
            if (
                QtWidgets.QMessageBox.question(
                    self,
                    "采集完成",
                    "连续 BIN 已保存。是否转换为 BDF 或 MNE FIF？",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                == QtWidgets.QMessageBox.Yes
            ):
                self.export_biosignal_formats(first_path)

    def enqueue_raw_bytes(self, data: bytes):
        """Queue raw bytes; continuous writing and metadata stay off Qt."""
        if not self.raw_recording_enabled or not data:
            return
        if self.raw_writer.submit(data):
            self.raw_bytes += len(data)
            snap = self.raw_writer.snapshot()
            current_path = str(snap.get("current_path", ""))
            if current_path:
                self.raw_path = current_path
            self.recording_segment_index = int(
                snap.get("segment_index", self.recording_segment_index)
            )
            return
        self.raw_recording_enabled = False
        self.raw_write_errors += 1
        self.raw_file = None
        error = self.raw_writer.error or "原始 BIN 写盘线程不可用"
        self.log_event("raw_writer_error", level="error", message=error)
        self.set_status(f"{error}；实时波形和传输继续运行。")

    def close_raw_file(self):
        if self.raw_recording_enabled or self.raw_file is not None:
            self.raw_recording_enabled = False
            try:
                self.raw_writer.stop(timeout=15.0)
            except Exception:
                self.raw_write_errors += 1
        snap = self.raw_writer.snapshot()
        current_path = str(snap.get("current_path", ""))
        if current_path:
            self.raw_path = current_path
        self.recording_manifest_path = str(snap.get("manifest_path", self.recording_manifest_path))
        self.recording_segment_index = int(snap.get("segment_index", self.recording_segment_index))
        self.raw_file = None

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
                self.set_status(
                    f"已发送 PGA={new_gain}；显示 LSB 同步为 {self.lsb_uv:.6g} uV/code。"
                )
            except Exception as exc:
                self.streaming = False
                self.set_status(f"PGA 指令发送失败：{exc}")
        else:
            self.set_status(f"仅修改本地解码 PGA={new_gain}，LSB={self.lsb_uv:.6g} uV/code。")

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
        delay = (
            SERIAL_REPOLL_DELAY_MS
            if self.active_transport == "serial"
            else TRANSPORT_REPOLL_DELAY_MS
        )
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
                    self.serial_worker.queued_data_bytes() if self.serial_worker is not None else 0
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
                    0.0
                    if self.ble_batch_started_monotonic is None
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
                    0.0
                    if self.ble_batch_started_monotonic is None
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

    def process_frames(self, frames: List[Frame], live: bool):
        if not frames:
            return
        now = time.perf_counter()
        detected_reference = None
        previous_sequence = self.last_seq
        previous_mode = int(self.current_mode)
        use_ble_timeline = bool(live and self.active_transport == "ble")

        if use_ble_timeline:
            (
                timeline_values,
                timeline_valid,
                timeline_sequence,
                timeline_modes,
                _lost_samples,
                filled_samples,
                gap_events,
                large_discontinuities,
                _last_timeline_seq,
                _last_timeline_mode,
            ) = expand_frames_to_timeline(
                frames,
                previous_sequence=previous_sequence,
                previous_mode=previous_mode,
            )
            self.timeline_gap_samples += int(filled_samples)
            self.timeline_gap_events += int(gap_events)
            self.timeline_large_discontinuities += int(large_discontinuities)
        elif live:
            # Proven serial behavior: append only bytes actually received.  Do
            # not allocate NaN columns on the hot USB path; sequence accounting
            # remains exact in the loop below.
            timeline_values = np.stack([fr.uv for fr in frames], axis=1).astype(np.float32)
            timeline_valid = np.array([fr.valid for fr in frames], dtype=bool)
            timeline_sequence = np.array([fr.sequence for fr in frames], dtype=np.uint32)
            timeline_modes = np.array([fr.mode for fr in frames], dtype=np.uint8)
            large_discontinuities = 0
        else:
            timeline_values = timeline_valid = timeline_sequence = timeline_modes = None
            large_discontinuities = 0

        for fr in frames:
            self.packet_count += 1
            if not (fr.flags & 0x01):
                self.status_bad += 1
            if not (fr.flags & 0x02):
                self.drdy_bad += 1
            if (fr.flags & 0x04) or fr.pending > 1:
                self.backlog_events += 1

            drop_delta = (fr.queue_drop_low - self.last_queue_drop_low) & 0xFF
            if self.packet_count > 1 and 0 < drop_delta < 128:
                self.queue_drop_hints += drop_delta
            self.last_queue_drop_low = fr.queue_drop_low

            gap = sequence_gap_size(self.last_seq, fr.sequence)
            if gap:
                previous_for_log = self.last_seq
                self.seq_lost += gap
                self.seq_gap_events += 1
                # pending/backlog and queue-drop counters are generated inside
                # the C3.  Without either hint, the most likely loss point is
                # host USB/BLE reception or parser resynchronisation.
                if fr.pending > 1 or bool(fr.flags & 0x04) or (0 < drop_delta < 128):
                    self.seq_device_lost += gap
                else:
                    self.seq_host_lost += gap
                self.log_event(
                    "sample_sequence_gap",
                    level="error",
                    previous_sequence=previous_for_log,
                    current_sequence=int(fr.sequence),
                    lost_samples=int(gap),
                    pending=int(fr.pending),
                    queue_drop_delta=int(drop_delta),
                    frame_flags=int(fr.flags),
                )

            if self.last_seq is None:
                self.first_seq = fr.sequence
                self.first_clock = now
            self.last_seq = fr.sequence
            if live and self.first_clock is not None and self.first_seq is not None:
                elapsed = now - self.first_clock
                if elapsed > 1:
                    progressed = ((fr.sequence - self.first_seq) & 0xFFFFFFFF) + 1
                    self.fs_est = progressed / elapsed

            frame_saturated = np.abs(fr.raw_counts) >= (ADC_SATURATION_FRACTION * (2**23 - 1))
            enabled_saturated = frame_saturated & self.channel_enabled
            self.saturation_samples += int(np.sum(enabled_saturated))
            self.saturation_channel_samples += enabled_saturated.astype(np.int64)
            self.current_mode = fr.mode
            if fr.mode in (0, 1, 2):
                detected_reference = REFERENCE_SRB1 if (fr.flags & 0x80) else REFERENCE_SRB2
            self.last_read_us = fr.read_us
            self.max_read_us = max(self.max_read_us, fr.read_us)
            self.last_pending = fr.pending
            self.last_queue_depth = fr.queue_depth

        if live:
            self.live_sample_count += len(frames)
            timeline_count = int(timeline_values.shape[1])
            self.live_timeline_sample_count += timeline_count
            self.live_lag_s = max(
                float(self.last_pending) / FS,
                float(self.last_queue_depth) / FS,
                float(self.last_serial_waiting_bytes) / BYTES_PER_SECOND,
            )
            self.ring.append_batch(
                timeline_values, timeline_valid, timeline_sequence, timeline_modes
            )
            if self.stream_server is not None and timeline_count:
                self.stream_server.set_first_sequence(int(timeline_sequence[0]))
            self._publish_stream_batch(
                STREAM_RAW,
                timeline_values,
                timeline_valid,
                timeline_sequence,
                timeline_modes,
                generation=None,
            )
            # Raw ring/BIN keep the exact ADC rail values. For the live filter
            # and screen copy only, isolate rail samples per channel with NaN.
            # Otherwise a floating BIAS/electrode can alternate between +/-FS and
            # force pyqtgraph to draw thousands of full-height vertical segments,
            # starving the BLE decoder/ACK path even though radio throughput is OK.
            filter_values = np.asarray(timeline_values, dtype=np.float32).copy()
            saturation_matrix = self.saturation_mask_uv(filter_values)
            if np.any(saturation_matrix):
                filter_values[saturation_matrix] = np.nan
            if self.filter_worker is not None:
                self.filter_worker.submit(
                    self.filter_generation,
                    filter_values,
                    timeline_valid,
                    timeline_sequence,
                    timeline_modes,
                )
            else:
                self.append_live_filtered(
                    frames,
                    filter_values,
                    timeline_valid,
                    timeline_sequence,
                    timeline_modes,
                )
            if use_ble_timeline and large_discontinuities:
                self.display_cursor_sample = None
                self.display_buffer_started = False
                self.display_buffer_state = "priming"

        if detected_reference is not None and detected_reference != self.reference_mode:
            self.set_reference_mode_local(detected_reference)
        self._sync_internal_short_button(self.current_mode == 3)

    def drain_filter_results(self):
        worker = self.filter_worker
        if worker is None:
            return
        deadline = time.perf_counter() + FILTER_RESULT_BUDGET_S
        for batch in worker.drain(max_batches=32):
            if batch.generation != self.filter_generation:
                self.filter_stale_batches += 1
                continue
            self.filtered_ring.append_batch(
                batch.filtered, batch.valid, batch.sequence, batch.modes
            )
            self._publish_stream_batch(
                STREAM_FILTERED,
                batch.filtered,
                batch.valid,
                batch.sequence,
                batch.modes,
                generation=batch.generation,
            )
            self.filter_batches_applied += 1
            if time.perf_counter() >= deadline:
                break

    def _publish_stream_batch(
        self,
        stream: str,
        values: np.ndarray,
        valid: np.ndarray,
        sequence: np.ndarray,
        modes: np.ndarray,
        generation: Optional[int],
    ) -> None:
        server = self.stream_server
        if server is None:
            return
        try:
            publish_gui_matrix(
                server,
                stream=stream,
                values=values,
                sequence=sequence,
                valid=valid,
                modes=modes,
                generation=generation,
                session_id=server.session_id,
            )
        except Exception as exc:
            self.stream_api_errors += 1
            if self.stream_api_errors == 1:
                print(f"Local EEG stream publish disabled: {exc}", file=sys.stderr)
                self.stream_server = None

    def filter_worker_backlog_samples(self) -> int:
        worker = self.filter_worker
        if worker is None:
            return 0
        metrics = worker.metrics()
        return int(metrics["queued_samples"] + metrics["output_samples"])

    def current_bias_mask(self) -> int:
        mask = 0
        for i, cb in enumerate(self.bias_checks):
            if cb.isChecked() and self.channel_enabled[i]:
                mask |= 1 << i
        return mask & 0xFF

    def update_bias_mask_label(self):
        self.bias_mask_label.setText(f"mask=0x{self.current_bias_mask():02X}")

    def set_bias_checks(self, mask: int):
        enabled_mask = sum((1 << i) for i in range(CHANNELS) if self.channel_enabled[i])
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
                raise RuntimeError("固件未返回 A6 寄存器读回。请烧录带配置 ACK 的最新固件。")
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
