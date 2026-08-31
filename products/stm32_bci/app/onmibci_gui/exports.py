"""Main-window behavior grouped by responsibility."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace


class ExportMixin:
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
                self.build_mne_raw().save(fif_path, overwrite=True, fmt="double", verbose="ERROR")
                written.append(fif_path)
            names = "\n".join(str(path) for path in written)
            self.set_status(f"已导出：{', '.join(path.name for path in written)}")
            QtWidgets.QMessageBox.information(self, "导出完成", f"已生成：\n{names}")
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
                headers.append(
                    {
                        "label": self.channel_names[ch],
                        "dimension": "uV",
                        "sample_frequency": FS,
                        "physical_min": -physical_peak,
                        "physical_max": physical_peak,
                        "digital_min": -8388608,
                        "digital_max": 8388607,
                        "transducer": "ADS1299",
                        "prefilter": "Raw, unfiltered",
                    }
                )
            writer.setSignalHeaders(headers)
            writer.setPatientCode("")
            writer.setEquipment("ADS1299")
            writer.writeSamples(
                [np.nan_to_num(data[ch], nan=0.0, posinf=0.0, neginf=0.0) for ch in range(CHANNELS)]
            )
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

    def _write_bdf_data(
        self,
        path: Path,
        data: np.ndarray,
        valid: np.ndarray,
        *,
        markers: tuple[MarkerEvent, ...] = tuple(),
        recording_started_at: float | None = None,
        first_sequence: int | None = None,
        overwrite: bool = False,
    ) -> None:
        if not overwrite and Path(path).exists():
            raise FileExistsError(f"BDF output already exists: {path}")
        data = np.asarray(data, dtype=np.float64)
        valid = np.asarray(valid, dtype=bool)
        if data.ndim != 2 or data.shape[0] != CHANNELS or data.shape[1] <= 0:
            raise ValueError("BDF data must have shape (8, samples)")
        if valid.ndim != 1 or valid.size != data.shape[1]:
            raise ValueError("BDF validity length must match data")
        if markers and recording_started_at is None:
            raise ValueError("recording_started_at is required for marker export")
        try:
            import pyedflib
        except ImportError as exc:
            raise RuntimeError("缺少 BDF 依赖 pyedflib，请运行：uv sync --extra export") from exc

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
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
                headers.append(
                    {
                        "label": self.channel_names[ch],
                        "dimension": "uV",
                        "sample_frequency": FS,
                        "physical_min": -physical_peak,
                        "physical_max": physical_peak,
                        "digital_min": -8388608,
                        "digital_max": 8388607,
                        "transducer": "ADS1299",
                        "prefilter": "Raw, unfiltered",
                    }
                )
            writer.setSignalHeaders(headers)
            writer.setPatientCode("")
            writer.setEquipment("ADS1299")
            writer.writeSamples(
                [np.nan_to_num(data[ch], nan=0.0, posinf=0.0, neginf=0.0) for ch in range(CHANNELS)]
            )
            annotations = []
            remainder = data.shape[1] % FS
            # A duration annotation covering the padded tail can hide later
            # user markers in some BDF+ readers. The writer still performs
            # the required record padding; keep this legacy annotation only
            # for exports without user events.
            if remainder and not markers:
                annotations.append(
                    (
                        float(data.shape[1]) / FS,
                        float(FS - remainder) / FS,
                        "BDF_padding",
                    )
                )

            if not valid.all():
                padded = np.r_[False, ~valid, False].astype(np.int8)
                edges = np.diff(padded)
                starts = np.flatnonzero(edges == 1)
                ends = np.flatnonzero(edges == -1)
                for start, end in zip(starts, ends):
                    annotations.append(
                        (
                            float(start) / FS,
                            float(end - start) / FS,
                            "BAD_frame",
                        )
                    )
            for marker in markers:
                onset, duration, text = bdf_annotation_for_marker(
                    marker,
                    recording_started_at=float(recording_started_at),
                    first_sequence=first_sequence,
                    sample_rate=FS,
                    sample_count=int(data.shape[1]),
                )
                annotations.append((onset, duration, text))
            for onset, duration, text in sorted(annotations, key=lambda item: item[0]):
                writer.writeAnnotation(onset, duration, text)
        finally:
            writer.close()

    def export_recording_bdf(
        self,
        path: Path,
        markers: tuple[MarkerEvent, ...],
        *,
        recording_id: str,
        recording_started_at: float,
        first_sequence: int | None,
        overwrite: bool = False,
    ) -> dict:
        """Export every completed BIN segment from the last live recording."""

        if not isinstance(recording_id, str) or not recording_id:
            raise ValueError("recording_id must not be empty")
        path = Path(path)
        snapshot = self.raw_writer.snapshot()
        segment_records = snapshot.get("segments", [])
        segment_paths = [
            Path(record["path"])
            for record in segment_records
            if isinstance(record, dict) and isinstance(record.get("path"), str)
        ]
        if not segment_paths:
            first_path = snapshot.get("first_path")
            if isinstance(first_path, str) and first_path:
                segment_paths = [Path(first_path)]
        segment_paths = [segment for segment in segment_paths if segment.exists()]
        if not segment_paths:
            raise RuntimeError("no_completed_recording")

        parser = AdsFrameParser(self.channel_lsb_uv)
        frames: list[Frame] = []
        for segment_path in segment_paths:
            frames.extend(parser.feed(segment_path.read_bytes()))
        if not frames:
            raise RuntimeError("recording_contains_no_valid_frames")

        (
            data,
            valid,
            sequence,
            _modes,
            _lost,
            _filled,
            _gap_events,
            _large_discontinuities,
            _last_sequence,
            _last_mode,
        ) = expand_frames_to_timeline(
            frames,
            previous_sequence=None,
            previous_mode=int(frames[0].mode),
        )
        if first_sequence is None:
            first_sequence = int(sequence[0])
        for marker in markers:
            if marker.recording_id != recording_id:
                raise ValueError("marker recording_id does not match export")
        self._write_bdf_data(
            path,
            data,
            valid,
            markers=tuple(markers),
            recording_started_at=recording_started_at,
            first_sequence=first_sequence,
            overwrite=overwrite,
        )
        return {
            "path": str(path.resolve()),
            "recording_id": recording_id,
            "event_count": len(markers),
            "sample_count": int(data.shape[1]),
        }

    def build_mne_raw(self):
        """Build an unfiltered MNE RawArray from the currently imported file."""
        if self.offline_uv is None:
            raise RuntimeError("请先导入一个 BIN 或 BDF 文件。")

        import mne

        channel_names = [f"CH{i}" for i in range(1, CHANNELS + 1)]
        info = mne.create_info(channel_names, FS, ch_types=[MNE_CHANNEL_TYPE] * CHANNELS)
        info["line_freq"] = 50.0
        info["description"] = (
            f"ADS1299 GUI file import: {Path(getattr(self, 'loaded_path', 'unknown')).name}"
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
        matrix = np.column_stack((time_s, self.offline_uv.astype(np.float64).T * 1e-6))
        header = "time_s," + ",".join(f"CH{i}_V" for i in range(1, CHANNELS + 1))
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
                duration=self.win_spin.value(),
                scalings={"eeg": self.sensitivity_spin.value() * 1e-6},
                block=False,
                title=Path(getattr(self, "loaded_path", "ADS1299")).name,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "MNE 浏览器", str(exc))
