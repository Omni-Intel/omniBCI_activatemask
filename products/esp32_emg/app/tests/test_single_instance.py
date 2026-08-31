import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QLockFile


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_owner_is_rejected_until_first_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "omniemg-v20.instance.lock")
            first = QLockFile(path)
            second = QLockFile(path)

            try:
                self.assertTrue(first.tryLock(0))
                self.assertFalse(second.tryLock(0))
                first.unlock()
                self.assertTrue(second.tryLock(0))
            finally:
                first.unlock()
                second.unlock()


if __name__ == "__main__":
    unittest.main()
