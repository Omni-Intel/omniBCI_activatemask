import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from verify_sources import verify_sources


class SourceIntegrityTests(unittest.TestCase):
    def test_modified_missing_and_binary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            content = b"\x00\xff\x01"
            (root / "firmware.bin").write_bytes(content)
            blob = hashlib.sha1(b"blob 3\0" + content).hexdigest()
            manifest = {"snapshots": [{"files": {"firmware.bin": blob}}]}
            self.assertEqual(verify_sources(root, manifest), [])
            (root / "firmware.bin").write_bytes(b"wrong")
            self.assertEqual(verify_sources(root, manifest), ["modified: firmware.bin"])
            (root / "firmware.bin").unlink()
            self.assertEqual(verify_sources(root, manifest), ["missing: firmware.bin"])

    def test_crlf_checkout_is_not_a_code_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=root, check=True)
            (root / "main.py").write_bytes(b"pass\r\n")
            blob = hashlib.sha1(b"blob 5\0pass\n").hexdigest()
            self.assertEqual(verify_sources(root, {"snapshots": [{"files": {"main.py": blob}}]}), [])

    def test_rejects_paths_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                verify_sources(Path(directory), {"snapshots": [{"files": {"../outside": "0" * 40}}]})

    def test_source_inventory_and_canonical_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            content = b"\0firmware"
            (root / "firmware.bin").write_bytes(content)
            subprocess.run(["git", "add", "firmware.bin"], cwd=root, check=True)
            tree = subprocess.check_output(["git", "write-tree"], cwd=root, text=True).strip()
            blob = subprocess.check_output(["git", "hash-object", "firmware.bin"], cwd=root, text=True).strip()
            group = {"name": "test", "commit": tree, "source_paths": [],
                     "destination_prefix": "", "files": {"firmware.bin": blob}}
            manifest = {"snapshots": [group], "firmware_canonical_sha256": {
                "firmware.bin": hashlib.sha256(content).hexdigest()}}
            self.assertEqual(verify_sources(root, manifest), [])
            manifest["firmware_canonical_sha256"]["firmware.bin"] = "0" * 64
            self.assertIn("SHA-256 mismatch: firmware.bin", verify_sources(root, manifest))
            group["files"] = {}
            self.assertIn("source inventory mismatch: test", verify_sources(root, manifest))
            group["commit"] = "0" * 40
            with self.assertRaises(subprocess.CalledProcessError):
                verify_sources(root, manifest)


if __name__ == "__main__":
    unittest.main()
