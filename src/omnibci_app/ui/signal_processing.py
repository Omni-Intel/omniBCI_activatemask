"""SignalProcessing behavior for the main window."""

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
    ADC_SATURATION_FRACTION,
    BYTES_PER_SECOND,
    CHANNELS,
    FILTER_RESULT_BUDGET_S,
    FS,
    REFERENCE_SRB1,
    REFERENCE_SRB2,
    VREF,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline, sequence_gap_size
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class SignalProcessingMixin:
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
            use_notch = (
                self.notch_check.isChecked()
                if hasattr(self, "notch_check") else True
            )
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
        filtered = np.vstack([
            self.filter_offline_display(values[ch], valid)
            for ch in range(CHANNELS)
        ])
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
                    self.display_zi_band[ch] = signal.sosfilt_zi(self.sos_display_band) * first_value
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
        filtered, band_zf = signal.sosfilt(
            self.sos_display_band, filled, axis=1, zi=band_zi
        )
        self.display_zi_band = np.transpose(band_zf, (1, 0, 2))
        if not hasattr(self, "notch_check") or self.notch_check.isChecked():
            notch_zi = np.transpose(self.display_zi_notch, (1, 0, 2))
            filtered, notch_zf = signal.sosfilt(
                self.sos_notch, filtered, axis=1, zi=notch_zi
            )
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
            seed = float(self.last_filter_input[ch]) if np.isfinite(self.last_filter_input[ch]) else 0.0
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
            cleaned, fs=FS, window="hann", nperseg=nperseg,
            noverlap=noverlap, nfft=nfft, detrend=False,
        )
        display_signal = self.filter_for_psd(
            cleaned, sos_band=sos_band, use_notch=use_notch
        )
        display_f, display_p = signal.welch(
            display_signal, fs=FS, window="hann", nperseg=nperseg,
            noverlap=noverlap, nfft=nfft,
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
            reason = f"PSD持续计算；饱和样本 {sat_ratio*100:.1f}%"
            return False, reason, metrics
        if valid_ratio < 0.99 or max_gap > 2:
            return False, f"PSD持续计算；有效样本 {valid_ratio*100:.1f}%", metrics
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
        display_signal = self.filter_for_psd(
            cleaned_all, sos_band=sos_band, use_notch=use_notch
        )
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
            metrics["alpha_peak"] = float(
                display_af[int(np.argmax(display_p[display_alpha]))]
            )
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
            alpha_signal = self.filter_for_psd(
                segment_x, sos_band=sos_band, use_notch=use_notch
            )
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
                self.seq_lost += gap
                self.seq_gap_events += 1
                # pending/backlog and queue-drop counters are generated inside
                # the C3.  Without either hint, the most likely loss point is
                # host USB/BLE reception or parser resynchronisation.
                if fr.pending > 1 or bool(fr.flags & 0x04) or (0 < drop_delta < 128):
                    self.seq_device_lost += gap
                else:
                    self.seq_host_lost += gap

            if self.last_seq is None:
                self.first_seq = fr.sequence
                self.first_clock = now
            self.last_seq = fr.sequence
            if live and self.first_clock is not None and self.first_seq is not None:
                elapsed = now - self.first_clock
                if elapsed > 1:
                    progressed = ((fr.sequence - self.first_seq) & 0xFFFFFFFF) + 1
                    self.fs_est = progressed / elapsed

            frame_saturated = np.abs(fr.raw_counts) >= (
                ADC_SATURATION_FRACTION * (2**23 - 1)
            )
            enabled_saturated = frame_saturated & self.channel_enabled
            self.saturation_samples += int(np.sum(enabled_saturated))
            self.saturation_channel_samples += enabled_saturated.astype(np.int64)
            self.current_mode = fr.mode
            if fr.mode in (0, 1, 2):
                detected_reference = (
                    REFERENCE_SRB1 if (fr.flags & 0x80) else REFERENCE_SRB2
                )
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
            self.filter_batches_applied += 1
            if time.perf_counter() >= deadline:
                break


    def filter_worker_backlog_samples(self) -> int:
        worker = self.filter_worker
        if worker is None:
            return 0
        metrics = worker.metrics()
        return int(metrics["queued_samples"] + metrics["output_samples"])
