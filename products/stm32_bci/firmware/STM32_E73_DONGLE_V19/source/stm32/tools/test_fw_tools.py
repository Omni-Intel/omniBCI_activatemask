import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parent
SCRIPT = TOOLS_DIR / "fw.ps1"


class FirmwareToolTests(unittest.TestCase):
    def run_doctor(
        self,
        nrfutil: Path,
        jlink: Path,
        ncs_root: Path,
        toolchain_root: Path,
        hal_stm32: Path,
        mcumgr: Path,
    ):
        env = os.environ.copy()
        env["STM32_FW_NRFUTIL"] = str(nrfutil)
        env["STM32_FW_JLINK"] = str(jlink)
        env["STM32_FW_NCS_ROOT"] = str(ncs_root)
        env["STM32_FW_TOOLCHAIN_ROOT"] = str(toolchain_root)
        env["HAL_STM32_PATH"] = str(hal_stm32)
        env["STM32_FW_MCUMGR"] = str(mcumgr)
        env["STM32_FW_SKIP_TOOL_EXEC"] = "1"
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
            mcumgr = root / "mcumgr.exe"
            mcumgr.touch()
            ncs_root = root / "ncs" / "v3.4.0"
            (ncs_root / ".west").mkdir(parents=True)
            (ncs_root / "zephyr").mkdir()
            toolchain_root = root / "ncs" / "toolchains" / "fake"
            toolchain_root.mkdir(parents=True)
            (toolchain_root / "manifest.json").write_text("{}", encoding="utf-8")
            hal_stm32 = root / "hal_stm32"
            hal_stm32.mkdir()
            (hal_stm32 / "zephyr").mkdir()

            result = self.run_doctor(
                nrfutil, jlink, ncs_root, toolchain_root, hal_stm32, mcumgr
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["ready"])
            self.assertEqual(report["requiredNcsVersion"], "v3.4.0")
            self.assertEqual(Path(report["nrfutil"]["path"]), nrfutil.resolve())
            self.assertEqual(Path(report["jlink"]["path"]), jlink.resolve())
            self.assertEqual(Path(report["ncs"]["path"]), ncs_root.resolve())
            self.assertEqual(
                Path(report["toolchain"]["path"]), toolchain_root.resolve()
            )
            self.assertEqual(Path(report["halStm32"]["path"]), hal_stm32.resolve())
            self.assertEqual(Path(report["mcumgr"]["path"]), mcumgr.resolve())

    def test_doctor_fails_when_jlink_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nrfutil = root / "nrfutil.exe"
            nrfutil.touch()

            result = self.run_doctor(
                nrfutil,
                root / "missing-JLink.exe",
                root / "ncs",
                root / "toolchain",
                root / "hal_stm32",
                root / "mcumgr.exe",
            )

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertFalse(report["ready"])
            self.assertFalse(report["jlink"]["available"])

    def test_doctor_fails_when_ncs_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nrfutil = root / "nrfutil.exe"
            jlink = root / "JLink.exe"
            nrfutil.touch()
            jlink.touch()
            mcumgr = root / "mcumgr.exe"
            mcumgr.touch()
            toolchain = root / "toolchain"
            toolchain.mkdir()
            (toolchain / "manifest.json").write_text("{}", encoding="utf-8")
            hal_stm32 = root / "hal_stm32"
            (hal_stm32 / "zephyr").mkdir(parents=True)

            result = self.run_doctor(
                nrfutil,
                jlink,
                root / "missing-ncs",
                toolchain,
                hal_stm32,
                mcumgr,
            )

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertFalse(report["ready"])
            self.assertFalse(report["ncs"]["available"])


if __name__ == "__main__":
    unittest.main()
