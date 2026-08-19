import tempfile
import unittest
from pathlib import Path

from app_diagnostics import HangWatchdog, configure_logging, shutdown_logging


class AppDiagnosticsTests(unittest.TestCase):
    def test_configure_logging_creates_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            logger, path = configure_logging(Path(directory))
            logger.info("startup marker")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(path.exists())
            self.assertIn("startup marker", path.read_text(encoding="utf-8"))
            shutdown_logging(logger)

    def test_watchdog_dumps_once_per_hang_and_rearms_after_heartbeat(self):
        dumps = []
        watchdog = HangWatchdog(5.0, dumps.append, initial_time=10.0)

        watchdog.check_once(14.9)
        watchdog.check_once(15.1)
        watchdog.check_once(20.0)
        watchdog.heartbeat(21.0)
        watchdog.check_once(26.1)

        self.assertEqual(len(dumps), 2)
        self.assertGreaterEqual(dumps[0], 5.0)
        self.assertGreaterEqual(dumps[1], 5.0)


if __name__ == "__main__":
    unittest.main()
