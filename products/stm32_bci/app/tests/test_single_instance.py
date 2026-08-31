import tempfile
import unittest
from pathlib import Path

from onmibci_gui.single_instance import SingleInstanceLock


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_owner_is_rejected_until_first_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gui.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)

            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire(timeout_ms=0))
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()

    def test_acquire_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = SingleInstanceLock(Path(directory) / "gui.lock")
            try:
                self.assertTrue(lock.acquire())
                self.assertTrue(lock.acquire())
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()
