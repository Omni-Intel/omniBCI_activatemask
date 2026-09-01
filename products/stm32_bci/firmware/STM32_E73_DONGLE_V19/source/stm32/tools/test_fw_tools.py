import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parent
SCRIPT = TOOLS_DIR / "fw.ps1"


class FirmwareToolTests(unittest.TestCase):
    def run_doctor(self, nrfutil: Path, jlink: Path):
        env = os.environ.copy()
        env["STM32_FW_NRFUTIL"] = str(nrfutil)
        env["STM32_FW_JLINK"] = str(jlink)
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(SCRIPT),
                "doctor",
                "-Json",
            ],
            cwd=TOOLS_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

    def test_doctor_reports_injected_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nrfutil = root / "nrfutil.exe"
            jlink = root / "JLink.exe"
            nrfutil.touch()
            jlink.touch()

            result = self.run_doctor(nrfutil, jlink)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["ready"])
            self.assertEqual(report["requiredNcsVersion"], "v3.4.0")
            self.assertEqual(Path(report["nrfutil"]["path"]), nrfutil.resolve())
            self.assertEqual(Path(report["jlink"]["path"]), jlink.resolve())

    def test_doctor_fails_when_jlink_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nrfutil = root / "nrfutil.exe"
            nrfutil.touch()

            result = self.run_doctor(nrfutil, root / "missing-JLink.exe")

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertFalse(report["ready"])
            self.assertFalse(report["jlink"]["available"])


if __name__ == "__main__":
    unittest.main()
