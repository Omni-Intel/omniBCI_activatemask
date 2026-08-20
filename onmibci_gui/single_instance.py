"""Cross-process single-instance guard for the desktop GUI."""

from __future__ import annotations

from pathlib import Path
import tempfile

from PySide6 import QtCore


class SingleInstanceLock:
    """Own a QLockFile for as long as one GUI process is running."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            temp_location = QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.TempLocation
            )
            temp_directory = Path(temp_location or tempfile.gettempdir())
            path = temp_directory / "omnibci_v18_gui.lock"
        self.path = Path(path)
        self._lock = QtCore.QLockFile(str(self.path))
        # QLockFile verifies the owner PID before removing a stale lock. A
        # modest timeout also recovers from power loss without allowing two
        # live processes to own the device at once.
        self._lock.setStaleLockTime(30_000)
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self, timeout_ms: int = 100) -> bool:
        if self._acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquired = self._lock.tryLock(max(0, int(timeout_ms)))
        return self._acquired

    def release(self) -> None:
        if not self._acquired:
            return
        self._lock.unlock()
        self._acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise RuntimeError("another OmniBCI GUI instance is already running")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
