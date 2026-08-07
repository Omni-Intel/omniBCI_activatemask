"""FileIO behavior for the main window."""

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
    FRAME_BYTES,
    FS,
    MNE_CHANNEL_TYPE,
    MODE_NAMES,
    RECORDINGS_DIR,
    REFERENCE_SRB1,
    REFERENCE_SRB2,
)
from ..processing import ClockAxisItem, FilteredBatch, LiveFilterWorker, PsdWorker, RingBuffer
from ..protocol import AdsFrameParser, Frame, expand_frames_to_timeline
from ..recording import AsyncRawWriter
from ..transports import (
    BLE_AVAILABLE, BLE_IMPORT_ERROR, BleTransportWorker, SerialTransportWorker,
)

class FileIOMixin:
    def export_csv(self):
        if self.offline_uv is None:
            QtWidgets.QMessageBox.information(self, "导出 CSV", "请先导入一个 BIN 或 BDF 文件。")
            return
        recordings_dir = RECORDINGS_DIR
        mne_dir = recordings_dir / "mne"
        mne_dir.mkdir(parents=True, exist_ok=True)
        source = Path(getattr(self, "loaded_path", self.raw_path or "ADS1299"))
        path = mne_dir / f"{source.stem}_mne.csv"
        header = "time_s," + ",".join(f"CH{i}_uV" for i in range(1, 9))
        matrix = np.column_stack((np.arange(self.offline_uv.shape[1]) / FS, self.offline_uv.T))
        np.savetxt(path, matrix, delimiter=",", header=header, comments="", fmt="%.7g")
        self.set_status(f"已导出 {path}")
        QtWidgets.QMessageBox.information(self, "导出完成", f"已生成：\n{path}")


    def export_biosignal_formats(self, source_path=None):
        """Interactively export the imported or just-recorded signal."""
        if isinstance(source_path, bool):
            source_path = None
        if self.streaming:
            QtWidgets.QMessageBox.information(
                self, "导出 BDF/FIF", "请先停止实时采集，再选择导出格式。"
            )
            return
        try:
            if source_path:
                self._load_bin_path(str(source_path))
            elif self.offline_uv is None:
                candidate = Path(self.raw_path) if self.raw_path else None
                if candidate and candidate.exists() and candidate.stat().st_size:
                    self._load_bin_path(str(candidate))
                else:
                    QtWidgets.QMessageBox.information(
                        self, "导出 BDF/FIF", "请先导入 BIN 或完成一次实时采集。"
                    )
                    return

            choices = ["BDF + MNE FIF", "BDF", "MNE FIF"]
            choice, ok = QtWidgets.QInputDialog.getItem(
                self,
                "选择导出格式",
                "请选择要生成的文件格式：",
                choices,
                0,
                False,
            )
            if not ok:
                return
            source = Path(getattr(self, "loaded_path", self.raw_path or "ADS1299.bin"))
            recordings_dir = RECORDINGS_DIR
            bdf_dir = recordings_dir / "bdf"
            fif_dir = recordings_dir / "fif"
            bdf_dir.mkdir(parents=True, exist_ok=True)
            fif_dir.mkdir(parents=True, exist_ok=True)
            stem = source.stem
            written = []
            if "BDF" in choice:
                bdf_path = bdf_dir / f"{stem}.bdf"
                self.save_bdf(bdf_path)
                written.append(bdf_path)
            if "FIF" in choice:
                fif_path = fif_dir / f"{stem}_raw.fif"
                self.build_mne_raw().save(
                    fif_path, overwrite=True, fmt="double", verbose="ERROR"
                )
                written.append(fif_path)
            names = "\n".join(str(path) for path in written)
            self.set_status(f"已导出：{', '.join(path.name for path in written)}")
            QtWidgets.QMessageBox.information(
                self, "导出完成", f"已生成：\n{names}"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "导出 BDF/FIF 失败", str(exc))


    def save_bdf(self, path: Path):
        """Write the unfiltered input-referred signal as 24-bit BDF+."""
        if self.offline_uv is None:
            raise RuntimeError("没有可导出的采样数据。")
        try:
            import pyedflib
        except ImportError as exc:
            raise RuntimeError(
                "缺少 BDF 依赖 pyedflib，请运行：py -3 -m pip install pyedflib"
            ) from exc

        path = Path(path)
        data = np.asarray(self.offline_uv, dtype=np.float64)
        writer = pyedflib.EdfWriter(
            str(path),
            CHANNELS,
            file_type=pyedflib.FILETYPE_BDFPLUS,
        )
        try:
            headers = []
            for ch in range(CHANNELS):
                finite = data[ch][np.isfinite(data[ch])]
                peak = float(np.max(np.abs(finite))) if finite.size else 1.0
                physical_peak = max(100, int(np.ceil(peak * 1.1)))
                headers.append({
                    "label": f"CH{ch + 1}",
                    "dimension": "uV",
                    "sample_frequency": FS,
                    "physical_min": -physical_peak,
                    "physical_max": physical_peak,
                    "digital_min": -8388608,
                    "digital_max": 8388607,
                    "transducer": "ADS1299",
                    "prefilter": "Raw, unfiltered",
                })
            writer.setSignalHeaders(headers)
            writer.setPatientCode("")
            writer.setEquipment("ADS1299")
            writer.writeSamples([
                np.nan_to_num(data[ch], nan=0.0, posinf=0.0, neginf=0.0)
                for ch in range(CHANNELS)
            ])
            remainder = data.shape[1] % FS
            if remainder:
                writer.writeAnnotation(
                    float(data.shape[1]) / FS,
                    float(FS - remainder) / FS,
                    "BDF_padding",
                )

            valid = np.asarray(self.offline_valid, dtype=bool)
            if valid.size == data.shape[1] and not valid.all():
                padded = np.r_[False, ~valid, False].astype(np.int8)
                edges = np.diff(padded)
                starts = np.flatnonzero(edges == 1)
                ends = np.flatnonzero(edges == -1)
                for start, end in zip(starts, ends):
                    writer.writeAnnotation(
                        float(start) / FS,
                        float(end - start) / FS,
                        "BAD_frame",
                    )
        finally:
            writer.close()


    def build_mne_raw(self):
        """Build an unfiltered MNE RawArray from the currently imported file."""
        if self.offline_uv is None:
            raise RuntimeError("请先导入一个 BIN 或 BDF 文件。")

        import mne

        channel_names = [f"CH{i}" for i in range(1, CHANNELS + 1)]
        info = mne.create_info(
            channel_names, FS, ch_types=[MNE_CHANNEL_TYPE] * CHANNELS
        )
        info["line_freq"] = 50.0
        info["description"] = (
            f"ADS1299 GUI file import: "
            f"{Path(getattr(self, 'loaded_path', 'unknown')).name}"
        )
        raw = mne.io.RawArray(
            self.offline_uv.astype(np.float64) * 1e-6,
            info,
            verbose="ERROR",
        )

        # Preserve CRC/validity information as standard MNE BAD annotations.
        valid = np.asarray(self.offline_valid, dtype=bool)
        if valid.size == self.offline_uv.shape[1] and not valid.all():
            bad = ~valid
            padded = np.r_[False, bad, False].astype(np.int8)
            edges = np.diff(padded)
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            annotations = mne.Annotations(
                onset=starts.astype(float) / FS,
                duration=(ends - starts).astype(float) / FS,
                description=["BAD_frame"] * len(starts),
            )
            raw.set_annotations(annotations)
        return raw


    def save_mne_exports(self) -> Tuple[Path, Path]:
        """Save automatic MNE interchange CSV and native FIF exports."""
        if self.offline_uv is None:
            raise RuntimeError("请先导入一个 BIN 或 BDF 文件。")

        recordings_dir = RECORDINGS_DIR
        mne_dir = recordings_dir / "mne"
        fif_dir = recordings_dir / "fif"
        mne_dir.mkdir(parents=True, exist_ok=True)
        fif_dir.mkdir(parents=True, exist_ok=True)

        source_stem = Path(getattr(self, "loaded_path", "ADS1299")).stem
        mne_csv_path = mne_dir / f"{source_stem}_mne.csv"
        fif_path = fif_dir / f"{source_stem}_raw.fif"

        # MNE stores EEG in volts, so this interchange CSV deliberately uses V.
        time_s = np.arange(self.offline_uv.shape[1], dtype=np.float64) / FS
        matrix = np.column_stack(
            (time_s, self.offline_uv.astype(np.float64).T * 1e-6)
        )
        header = "time_s," + ",".join(
            f"CH{i}_V" for i in range(1, CHANNELS + 1)
        )
        np.savetxt(
            mne_csv_path,
            matrix,
            delimiter=",",
            header=header,
            comments="",
            fmt="%.10g",
        )

        raw = self.build_mne_raw()
        raw.save(fif_path, overwrite=True, fmt="double", verbose="ERROR")
        return mne_csv_path, fif_path


    def open_mne_browser(self):
        if self.offline_uv is None:
            QtWidgets.QMessageBox.information(self, "MNE 浏览器", "请先导入一个 BIN 或 BDF 文件。")
            return
        try:
            self.build_mne_raw().plot(
                duration=self.win_spin.value(), scalings={"eeg": self.sensitivity_spin.value()*1e-6},
                block=False, title=Path(getattr(self, "loaded_path", "ADS1299")).name)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "MNE 浏览器", str(exc))


    def _recording_folder(self) -> Path:
        folder = RECORDINGS_DIR / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


    def _recording_configuration_snapshot(self) -> dict:
        reference = "SRB2" if self.reference_is_srb2() else "SRB1"
        transport = self.transport_description() if self.transport_connected() else "disconnected"
        return {
            "sample_rate_hz": FS,
            "frame_bytes": FRAME_BYTES,
            "transport": transport,
            "ble_device_name": self.ble_device_name or "",
            "ble_device_address": self.ble_device_address or "",
            "reference": reference,
            "reference_code": int(self.reference_mode),
            "mode_code": int(self.current_mode),
            "mode_name": MODE_NAMES.get(int(self.current_mode), "UNKNOWN"),
            "global_gain": int(self.gain),
            "channel_gains": [int(value) for value in self.channel_gains.tolist()],
            "channel_enabled": [bool(value) for value in self.channel_enabled.tolist()],
            "channel_bias": [bool(value) for value in self.channel_bias.tolist()],
            "channel_srb2": [bool(value) for value in self.channel_srb2.tolist()],
            "bias_register": self.bias_register_name(),
            "bias_mask": int(self.current_bias_mask()),
            "bias_mask_hex": f"0x{self.current_bias_mask():02X}",
        }


    def make_raw_path(self) -> str:
        """Preview the next automatic one-minute segment name."""
        now = datetime.now()
        return str(self._recording_folder() / f"{now:%m%d_%H%M}_xxxxxx_minute01.bin")


    def enqueue_raw_bytes(self, data: bytes):
        """Queue raw bytes; rotation and metadata remain off the Qt thread."""
        if not self.raw_recording_enabled or not data:
            return
        if self.raw_writer.submit(data):
            self.raw_bytes += len(data)
            snap = self.raw_writer.snapshot()
            current_path = str(snap.get("current_path", ""))
            if current_path:
                self.raw_path = current_path
            self.recording_segment_index = int(snap.get("segment_index", self.recording_segment_index))
            return
        self.raw_recording_enabled = False
        self.raw_write_errors += 1
        self.raw_file = None
        error = self.raw_writer.error or "原始 BIN 写盘线程不可用"
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


    def _finish_offline_load(self, path: str):
        """Update navigation and plots after any supported file is loaded."""
        self.offline_end = self.offline_uv.shape[1]
        self.offline_slider.setEnabled(True)
        self.offline_slider.setRange(1, self.offline_end)
        self.offline_slider.setValue(self.offline_end)
        self.offline_label.setText(
            f"{Path(path).name}: {self.offline_end / FS:.1f}s"
        )
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
            raw.info, eeg=True, eog=True, ecg=True, emg=True,
            misc=True, stim=False, exclude=[],
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
                data_uv, ((0, CHANNELS - source_channels), (0, 0)),
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
                    "" if np.isclose(source_sfreq, FS)
                    else f"，已从 {source_sfreq:g} Hz 重采样到 {FS} Hz"
                )
                pad_note = (
                    "" if channel_count == CHANNELS
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
                f"已导入 {path}，有效帧 {valid_count}/{total_count}，"
                f"CRC坏帧 {parser.crc_bad}。"
            )
            if QtWidgets.QMessageBox.question(
                self,
                "转换采集文件",
                "BIN 已导入。是否现在转换为 BDF 或 MNE FIF？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            ) == QtWidgets.QMessageBox.Yes:
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
            self.offline_label.setText(f"{self.offline_end/FS:.1f}/{self.offline_uv.shape[1]/FS:.1f}s")
        self.update_fast_plots()
