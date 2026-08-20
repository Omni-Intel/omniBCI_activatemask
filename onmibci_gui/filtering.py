"""Runtime component extracted from the legacy GUI."""

from __future__ import annotations

from .common import *  # noqa: F403
from .frames import *  # noqa: F403


class ClockAxisItem(pg.AxisItem):
    """EEG paper time axis formatted as HH:MM:SS."""

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            seconds = max(0, int(round(value)))
            hours, rem = divmod(seconds, 3600)
            minutes, secs = divmod(rem, 60)
            labels.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
        return labels


class PsdWorkerSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class PsdWorker(QtCore.QRunnable):
    """Run the relatively expensive PSD/quality calculation off the GUI thread."""

    def __init__(
        self,
        owner,
        request_id: int,
        x: np.ndarray,
        valid: np.ndarray,
        seq: np.ndarray,
        mode: np.ndarray,
        sos_band: np.ndarray,
        use_notch: bool,
        live_fast: bool = False,
    ):
        super().__init__()
        # MainWindow retains this object until its completion signal is
        # handled. Disabling Qt's auto-delete keeps the Python-owned signal
        # object alive long enough for the queued cross-thread delivery.
        self.setAutoDelete(False)
        self.owner = owner
        self.request_id = request_id
        self.x = np.asarray(x, dtype=float)
        self.valid = np.asarray(valid, dtype=bool)
        self.seq = np.asarray(seq, dtype=np.uint32)
        self.mode = np.asarray(mode, dtype=np.uint8)
        self.sos_band = np.asarray(sos_band, dtype=float).copy()
        self.use_notch = bool(use_notch)
        self.live_fast = bool(live_fast)
        self.signals = PsdWorkerSignals()

    @QtCore.Slot()
    def run(self):
        started = time.perf_counter()
        try:
            if self.live_fast:
                result = self.owner.compute_live_psd_fast(
                    self.x,
                    self.valid,
                    self.seq,
                    self.mode,
                    sos_band=self.sos_band,
                    use_notch=self.use_notch,
                )
            else:
                result = self.owner.compute_alpha_from_window(
                    self.x,
                    self.valid,
                    self.seq,
                    self.mode,
                    sos_band=self.sos_band,
                    use_notch=self.use_notch,
                )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.signals.finished.emit(
                self.request_id,
                (result, self.x, self.valid, elapsed_ms),
            )
        except Exception as exc:  # pragma: no cover - surfaced in the GUI
            self.signals.failed.emit(self.request_id, str(exc))


@dataclass
class FilteredBatch:
    generation: int
    filtered: np.ndarray
    valid: np.ndarray
    sequence: np.ndarray
    modes: np.ndarray


class LiveFilterWorker:
    """Own the continuous live IIR state outside the Qt GUI thread.

    Transport reception and raw BIN writing must never wait for filtering or
    painting. Input samples are processed in order; display results use a
    bounded ring-like queue so a hidden or blocked window cannot grow memory
    forever. The raw BIN path remains independent and lossless while healthy.
    """

    _STOP = object()

    def __init__(self, sos_band: np.ndarray, sos_notch: np.ndarray, use_notch: bool):
        self._in = queue.Queue()
        self._out = queue.Queue(maxsize=FILTER_OUTPUT_MAX_BATCHES)
        self._thread = threading.Thread(target=self._run, name="OmniBCI-LiveFilter", daemon=True)
        self._lock = threading.Lock()
        self._running = False
        self._queued_samples = 0
        self._output_samples = 0
        self.peak_queued_samples = 0
        self.batches_processed = 0
        self.errors = 0
        self.display_dropped_samples = 0
        self.last_error = ""
        self._generation = 0
        self._sos_band = np.asarray(sos_band, dtype=float).copy()
        self._sos_notch = np.asarray(sos_notch, dtype=float).copy()
        self._use_notch = bool(use_notch)
        self._reset_state()

    def start(self):
        if self._thread.is_alive():
            return
        self._running = True
        self._thread.start()

    def configure(
        self, generation: int, sos_band: np.ndarray, sos_notch: np.ndarray, use_notch: bool
    ):
        self._in.put(
            (
                "config",
                int(generation),
                np.asarray(sos_band, dtype=float).copy(),
                np.asarray(sos_notch, dtype=float).copy(),
                bool(use_notch),
            )
        )

    def submit(self, generation: int, values, valid, sequence, modes):
        values = np.asarray(values, dtype=np.float32).copy()
        valid = np.asarray(valid, dtype=bool).copy()
        sequence = np.asarray(sequence, dtype=np.uint32).copy()
        modes = np.asarray(modes, dtype=np.uint8).copy()
        n = int(values.shape[1]) if values.ndim == 2 else 0
        if n <= 0:
            return
        with self._lock:
            self._queued_samples += n
            self.peak_queued_samples = max(self.peak_queued_samples, self._queued_samples)
        self._in.put(("batch", int(generation), values, valid, sequence, modes))

    def drain(self, max_batches: int = 16):
        out = []
        for _ in range(max(1, int(max_batches))):
            try:
                batch = self._out.get_nowait()
            except queue.Empty:
                break
            out.append(batch)
            with self._lock:
                self._output_samples = max(0, self._output_samples - int(batch.filtered.shape[1]))
        return out

    def metrics(self):
        with self._lock:
            return {
                "queued_samples": int(self._queued_samples),
                "output_samples": int(self._output_samples),
                "peak_queued_samples": int(self.peak_queued_samples),
                "batches_processed": int(self.batches_processed),
                "errors": int(self.errors),
                "display_dropped_samples": int(self.display_dropped_samples),
                "last_error": str(self.last_error),
            }

    def shutdown(self, timeout: float = 3.0):
        if not self._thread.is_alive():
            return
        self._in.put(self._STOP)
        self._thread.join(max(0.1, float(timeout)))
        self._running = False

    def _reset_state(self):
        self._zi_band = np.zeros((CHANNELS, self._sos_band.shape[0], 2), dtype=float)
        self._zi_notch = np.zeros((CHANNELS, self._sos_notch.shape[0], 2), dtype=float)
        self._last_input = np.zeros(CHANNELS, dtype=float)
        self._have_input = np.zeros(CHANNELS, dtype=bool)

    def _filter(self, values: np.ndarray, valid: np.ndarray) -> np.ndarray:
        source = np.asarray(values, dtype=float)
        # ``source`` may contain per-channel NaNs inserted for ADC saturation.
        # Hold those samples only to evolve the IIR; keep a channel-specific
        # mask so they remain display gaps instead of invented EEG.
        channel_good_matrix = np.asarray(valid, dtype=bool)[None, :] & np.isfinite(source)
        filled = source.copy()
        for ch in range(CHANNELS):
            channel_good = channel_good_matrix[ch]
            if not self._have_input[ch]:
                first_candidates = np.flatnonzero(channel_good)
                if first_candidates.size:
                    first_idx = int(first_candidates[0])
                    first_value = float(filled[ch, first_idx])
                    filled[ch, :first_idx] = first_value
                    self._zi_band[ch] = signal.sosfilt_zi(self._sos_band) * first_value
                    self._zi_notch[ch].fill(0.0)
                    self._last_input[ch] = first_value
                    self._have_input[ch] = True
            n_samples = int(filled.shape[1])
            if not n_samples:
                continue
            if channel_good.all():
                self._last_input[ch] = float(filled[ch, -1])
                self._have_input[ch] = True
                continue
            original = filled[ch].copy()
            seed = float(self._last_input[ch]) if self._have_input[ch] else 0.0
            last_good_index = np.where(channel_good, np.arange(n_samples, dtype=np.int64), -1)
            np.maximum.accumulate(last_good_index, out=last_good_index)
            has_previous = last_good_index >= 0
            filled[ch, ~has_previous] = seed
            if np.any(has_previous):
                filled[ch, has_previous] = original[last_good_index[has_previous]]
            good_indices = np.flatnonzero(channel_good)
            if good_indices.size:
                self._last_input[ch] = float(original[int(good_indices[-1])])
                self._have_input[ch] = True

        band_zi = np.transpose(self._zi_band, (1, 0, 2))
        filtered, band_zf = signal.sosfilt(self._sos_band, filled, axis=1, zi=band_zi)
        self._zi_band = np.transpose(band_zf, (1, 0, 2))
        if self._use_notch:
            notch_zi = np.transpose(self._zi_notch, (1, 0, 2))
            filtered, notch_zf = signal.sosfilt(self._sos_notch, filtered, axis=1, zi=notch_zi)
            self._zi_notch = np.transpose(notch_zf, (1, 0, 2))

        bad_channels = np.flatnonzero(
            ~np.all(np.isfinite(filtered), axis=1)
            | ~np.all(np.isfinite(self._zi_band), axis=(1, 2))
            | ~np.all(np.isfinite(self._zi_notch), axis=(1, 2))
        )
        for ch in bad_channels:
            seed = float(self._last_input[ch]) if np.isfinite(self._last_input[ch]) else 0.0
            self._zi_band[ch] = signal.sosfilt_zi(self._sos_band) * seed
            self._zi_notch[ch].fill(0.0)
            filtered[ch] = np.nan_to_num(filtered[ch], nan=seed, posinf=seed, neginf=seed)

        # NaN gaps are much cheaper for pyqtgraph than thousands of alternating
        # full-scale vertical segments, and they isolate only the bad channel.
        filtered[~channel_good_matrix] = np.nan
        return np.asarray(filtered, dtype=np.float32)

    def _run(self):
        while True:
            item = self._in.get()
            if item is self._STOP:
                return
            kind = item[0]
            if kind == "config":
                _, generation, band, notch, use_notch = item
                self._generation = int(generation)
                self._sos_band = band
                self._sos_notch = notch
                self._use_notch = bool(use_notch)
                self._reset_state()
                continue
            _, generation, values, valid, sequence, modes = item
            n = int(values.shape[1])
            try:
                if int(generation) != self._generation:
                    continue
                filtered = self._filter(values, valid)
                batch = FilteredBatch(int(generation), filtered, valid, sequence, modes)
                try:
                    self._out.put_nowait(batch)
                except queue.Full:
                    # Display results are bounded like a ring buffer. Raw BIN
                    # bytes are already safe in the independent writer, so an
                    # inactive/blocked Qt window must not grow memory forever.
                    try:
                        dropped = self._out.get_nowait()
                        dropped_n = int(dropped.filtered.shape[1])
                    except queue.Empty:
                        dropped_n = 0
                    with self._lock:
                        self._output_samples = max(0, self._output_samples - dropped_n)
                        self.display_dropped_samples += dropped_n
                    self._out.put_nowait(batch)
                with self._lock:
                    self._output_samples += n
                    self.batches_processed += 1
            except Exception as exc:
                with self._lock:
                    self.errors += 1
                    self.last_error = str(exc)
                self._reset_state()
            finally:
                with self._lock:
                    self._queued_samples = max(0, self._queued_samples - n)
