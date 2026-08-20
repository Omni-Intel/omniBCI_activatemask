"""Runtime component extracted from the legacy GUI."""

from __future__ import annotations

from .common import *  # noqa: F403


class AsyncEventLogger:
    """Non-blocking JSONL event log for operation/performance correlation."""

    _STOP = object()

    def __init__(self, folder: Path):
        self._queue = queue.Queue(maxsize=4096)
        self._lock = threading.Lock()
        self._dropped = 0
        self._error = ""
        self._started_monotonic = time.monotonic()
        now = datetime.now()
        try:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / f"gui_{now:%Y%m%d_%H%M%S}_{os.getpid()}.jsonl"
        except Exception as exc:
            self.path = Path("")
            self._error = str(exc)
        self._thread = threading.Thread(target=self._run, name="OmniBCI-EventLogger", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._lock:
            return int(self._dropped)

    @property
    def error(self) -> str:
        with self._lock:
            return str(self._error)

    def log(self, event: str, level: str = "info", **fields) -> None:
        item = {
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "monotonic_s": round(time.monotonic() - self._started_monotonic, 6),
            "event": str(event),
            "level": str(level),
            "thread": threading.current_thread().name,
            **fields,
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def _run(self) -> None:
        if not str(self.path):
            return
        try:
            with open(self.path, "a", encoding="utf-8", buffering=1) as handle:
                while True:
                    item = self._queue.get()
                    if item is self._STOP:
                        break
                    handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            with self._lock:
                self._error = str(exc)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._queue.put(self._STOP, timeout=0.25)
        except queue.Full:
            return
        self._thread.join(max(0.1, float(timeout)))


class AsyncRawWriter:
    """Lossless background writer with one continuous BIN per recording.

    Transport/Qt only enqueue bytes. File open/write/flush and JSON metadata all
    happen in this worker. Keeping the handle open for the whole acquisition
    avoids periodic file creation/Defender scans and minute-boundary I/O bursts.
    """

    _STOP = object()

    def __init__(self):
        self._queue = queue.Queue(maxsize=RAW_WRITER_QUEUE_CHUNKS)
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._error: Optional[str] = None
        self._queued_bytes = 0
        self.peak_queued_bytes = 0
        self.bytes_written = 0
        self.dropped_bytes = 0
        self._folder = ""
        self._session_id = ""
        self._session_prefix = ""
        self._session_started_at = ""
        self._manifest_path = ""
        self._first_path = ""
        self._current_path = ""
        self._segment_index = 0
        self._segment_bytes = 0
        self._segments = []
        self._configuration = {}

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return int(self._queued_bytes)

    @property
    def current_path(self) -> str:
        with self._lock:
            return str(self._current_path)

    @property
    def first_path(self) -> str:
        with self._lock:
            return str(self._first_path)

    @property
    def manifest_path(self) -> str:
        with self._lock:
            return str(self._manifest_path)

    @property
    def session_id(self) -> str:
        with self._lock:
            return str(self._session_id)

    @property
    def segment_count(self) -> int:
        with self._lock:
            return int(len(self._segments))

    @property
    def segment_index(self) -> int:
        with self._lock:
            return int(self._segment_index)

    @property
    def segment_bytes(self) -> int:
        with self._lock:
            return int(self._segment_bytes)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "session_id": self._session_id,
                "session_prefix": self._session_prefix,
                "manifest_path": self._manifest_path,
                "first_path": self._first_path,
                "current_path": self._current_path,
                "segment_index": int(self._segment_index),
                "segment_bytes": int(self._segment_bytes),
                "segment_count": int(len(self._segments)),
                "segments": [dict(item) for item in self._segments],
                "bytes_written": int(self.bytes_written),
                "queued_bytes": int(self._queued_bytes),
            }

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def start_session(self, folder: str, configuration: Optional[dict] = None):
        self.stop(timeout=2.0)
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        session_id = secrets.token_hex(3)
        prefix = f"{now:%m%d_%H%M}_{session_id}"
        first_path = target / f"{prefix}.bin"
        manifest = target / f"{prefix}_manifest.json"
        self._queue = queue.Queue(maxsize=RAW_WRITER_QUEUE_CHUNKS)
        self._started.clear()
        with self._lock:
            self._error = None
            self._queued_bytes = 0
            self.peak_queued_bytes = 0
            self.bytes_written = 0
            self.dropped_bytes = 0
            self._folder = str(target)
            self._session_id = session_id
            self._session_prefix = prefix
            self._session_started_at = now.isoformat(timespec="seconds")
            self._manifest_path = str(manifest)
            self._first_path = str(first_path)
            self._current_path = str(first_path)
            self._segment_index = 0
            self._segment_bytes = 0
            self._segments = []
            self._configuration = dict(configuration or {})
        self._thread = threading.Thread(
            target=self._run, name="OmniBCI-ContinuousRawWriter", daemon=True
        )
        self._thread.start()
        if not self._started.wait(2.0):
            raise RuntimeError("连续 BIN 写盘线程启动超时")
        if self.error:
            raise RuntimeError(self.error)

    def submit(self, data: bytes) -> bool:
        payload = bytes(data)
        if not payload:
            return True
        thread = self._thread
        if thread is None or not thread.is_alive() or self.error:
            return False
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self.dropped_bytes += len(payload)
                if self._error is None:
                    self._error = "原始 BIN 写盘队列已满；实时显示继续，但本次 BIN 已停止保证完整"
            return False
        with self._lock:
            self._queued_bytes += len(payload)
            self.peak_queued_bytes = max(self.peak_queued_bytes, self._queued_bytes)
        return True

    def stop(self, timeout: float = 10.0):
        thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + max(0.5, float(timeout))
        while thread.is_alive():
            try:
                self._queue.put(self._STOP, timeout=0.05)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            with self._lock:
                if self._error is None:
                    self._error = "连续 BIN 写盘线程停止超时"
        else:
            self._thread = None

    def _segment_path(self, index: int) -> Path:
        # Retain the segment-oriented metadata API for old import/export code,
        # but a new recording now always has exactly one file/record.
        return Path(self._folder) / f"{self._session_prefix}.bin"

    @staticmethod
    def _sidecar_path(bin_path: Path) -> Path:
        return Path(bin_path).with_suffix(".meta.json")

    def _metadata_payload(self, status: str) -> dict:
        with self._lock:
            segments = [dict(item) for item in self._segments]
            return {
                "schema": RECORD_METADATA_SCHEMA,
                "status": status,
                "recording_id": self._session_id,
                "session_prefix": self._session_prefix,
                "session_started_at": self._session_started_at,
                "recording_mode": "continuous_single_file",
                "total_bytes": int(self.bytes_written),
                "total_complete_frames": int(self.bytes_written // FRAME_BYTES),
                "configuration_at_session_start": dict(self._configuration),
                "segments": segments,
            }

    def _persist_metadata(self, status: str):
        payload = self._metadata_payload(status)
        manifest = self.manifest_path
        if manifest:
            self._write_json_atomic(Path(manifest), payload)
        with self._lock:
            segments = [dict(item) for item in self._segments]
            session_id = self._session_id
            prefix = self._session_prefix
            started = self._session_started_at
            manifest_name = Path(self._manifest_path).name if self._manifest_path else ""
            configuration = dict(self._configuration)
        for record in segments[-1:]:
            sidecar = self._sidecar_path(Path(record["path"]))
            self._write_json_atomic(
                sidecar,
                {
                    "schema": RECORD_METADATA_SCHEMA,
                    "status": record.get("status", status),
                    "recording_id": session_id,
                    "session_prefix": prefix,
                    "session_started_at": started,
                    "manifest_file": manifest_name,
                    **record,
                    "configuration": configuration,
                },
            )

    def _open_segment(self):
        with self._lock:
            self._segment_index += 1
            index = self._segment_index
            path = self._segment_path(index)
            self._current_path = str(path)
            if not self._first_path:
                self._first_path = str(path)
            self._segment_bytes = 0
            record = {
                "segment_index": int(index),
                "minute_label": "continuous",
                "path": str(path),
                "file": path.name,
                "sidecar_file": self._sidecar_path(path).name,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "bytes": 0,
                "complete_frames": 0,
                "duration_seconds": 0.0,
                "status": "recording",
            }
            self._segments.append(record)
        return open(path, "wb", buffering=RAW_WRITER_BUFFER_BYTES)

    def _update_current_record(self, status: str = "recording"):
        with self._lock:
            if not self._segments:
                return
            current = self._segments[-1]
            current["bytes"] = int(self._segment_bytes)
            current["complete_frames"] = int(self._segment_bytes // FRAME_BYTES)
            current["duration_seconds"] = round(self._segment_bytes / max(1, BYTES_PER_SECOND), 3)
            current["status"] = status
            if status != "recording":
                current["ended_at"] = datetime.now().isoformat(timespec="seconds")

    def _write_payload(self, handle, payload: bytes):
        if handle is None:
            handle = self._open_segment()
        view = memoryview(payload)
        written = handle.write(view)
        if written != len(view):
            raise OSError(f"BIN 写入不完整：请求 {len(view)} 字节，实际 {written} 字节")
        with self._lock:
            self._segment_bytes += written
            self.bytes_written += written
            self._queued_bytes = max(0, self._queued_bytes - written)
        self._update_current_record("recording")
        return handle

    def _run(self):
        handle = None
        status = "complete"
        try:
            handle = self._open_segment()
            self._persist_metadata("recording")
            self._started.set()
            last_flush = time.monotonic()
            last_metadata = last_flush
            while True:
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if item is self._STOP:
                    break
                if item is not None:
                    handle = self._write_payload(handle, item)
                now = time.monotonic()
                if handle is not None and now - last_flush >= RAW_WRITER_FLUSH_INTERVAL_S:
                    handle.flush()
                    last_flush = now
                if now - last_metadata >= RECORD_METADATA_UPDATE_INTERVAL_S:
                    self._update_current_record("recording")
                    self._persist_metadata("recording")
                    last_metadata = now
            if handle is not None:
                handle.flush()
                handle.close()
                handle = None
            self._update_current_record("complete")
        except Exception as exc:
            status = "error"
            with self._lock:
                self._error = f"原始 BIN 写盘失败：{exc}"
            self._update_current_record("error")
        finally:
            self._started.set()
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            try:
                self._persist_metadata(status)
            except Exception as meta_exc:
                with self._lock:
                    if self._error is None:
                        self._error = f"BIN 元数据写入失败：{meta_exc}"
