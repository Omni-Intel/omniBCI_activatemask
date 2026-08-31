"""File logging and GUI hang diagnostics for OmniBCI."""

import faulthandler
import logging
import os
import platform
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_NAME = "onmibci.log"


def configure_logging(log_dir: Path):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_NAME
    logger = logging.getLogger("onmibci")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.info("application logging started")
    logger.info("python=%s executable=%s", sys.version.replace("\n", " "), sys.executable)
    logger.info("platform=%s pid=%s cwd=%s", platform.platform(), os.getpid(), os.getcwd())
    return logger, path


def shutdown_logging(logger):
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def dump_all_thread_stacks(log_dir: Path, unresponsive_seconds: float) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = log_dir / f"hang_{stamp}.log"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            f"OmniBCI GUI unresponsive for {unresponsive_seconds:.1f} seconds\n"
            f"pid={os.getpid()} python={sys.executable}\n\n"
        )
        handle.flush()
        faulthandler.dump_traceback(file=handle, all_threads=True)
    return path


class HangWatchdog:
    def __init__(self, timeout_seconds, on_hang, *, initial_time=None):
        self.timeout_seconds = float(timeout_seconds)
        self.on_hang = on_hang
        self._lock = threading.Lock()
        self._last_heartbeat = time.monotonic() if initial_time is None else float(initial_time)
        self._reported = False

    def heartbeat(self, now=None):
        with self._lock:
            self._last_heartbeat = time.monotonic() if now is None else float(now)
            self._reported = False

    def check_once(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            elapsed = now - self._last_heartbeat
            if elapsed < self.timeout_seconds or self._reported:
                return False
            self._reported = True
        self.on_hang(elapsed)
        return True

    def start(self, interval_seconds=1.0):
        stop_event = threading.Event()

        def run():
            while not stop_event.wait(interval_seconds):
                self.check_once()

        thread = threading.Thread(target=run, name="OmniBCI-HangWatchdog", daemon=True)
        thread.start()
        return stop_event
