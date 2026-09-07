import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parent
SCRIPT = TOOLS_DIR / "fw.ps1"


class FirmwareToolTests(unittest.TestCase):
    def test_stm32_trigger_marker_preserves_legacy_frame_fields(self):
        source = (TOOLS_DIR.parent / "src" / "main.c").read_text()
        self.assertIn("current_mode | (trigger_marker ? BIT(7) : 0U)", source)
        self.assertIn("status_take_record_event(&event)", source)
        self.assertIn("sd_recorder_event(event.number, sample_sequence", source)

    def test_failed_mount_forces_disk_cleanup_before_retry(self):
        source = (TOOLS_DIR.parent / "src" / "sd_recorder.c").read_text()
        failure = source.split("int err = fs_mount(&mount_point);", 1)[1].split(
            "if (mounted && state.running", 1
        )[0]
        self.assertIn("bool force = true;", failure)
        self.assertIn('disk_access_ioctl("SD", DISK_IOCTL_CTRL_DEINIT, &force)', failure)

    def run_build(self, version: str, root: Path):
        return subprocess.run(
            [
                "pwsh", "-NoProfile", "-File", str(SCRIPT), "build",
                "-Version", version,
                "-BuildDir", str(root / "构建 output"),
                "-KeyPath", str(root / "密钥 signing.pem"),
                "-DryRun", "-Json",
            ],
            cwd=TOOLS_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

    def test_build_rejects_old_and_malformed_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in ("19.1.0", "19.1", "v19.3.0", "19.3.0-beta"):
                result = self.run_build(version, root)
                self.assertNotEqual(result.returncode, 0, version)

    def test_build_dry_run_preserves_literal_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.run_build("19.3.0", root)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["version"], "19.3.0")
            self.assertEqual(Path(report["buildDir"]), (root / "构建 output").resolve())
            self.assertEqual(Path(report["keyPath"]), (root / "密钥 signing.pem").resolve())
            self.assertIn(f"-d", report["westArguments"])
            self.assertIn(str((root / "构建 output").resolve()), report["westArguments"])
            self.assertTrue(report["stagedProject"].endswith("source-stage-dryrun\\stm32"))
            self.assertIn(report["stagedProject"], report["westArguments"])

    def test_mcuboot_compatibility_is_scoped_to_mcuboot(self):
        project = TOOLS_DIR.parent
        board_cmake = (
            project / "boards" / "bciband_h563vg" / "CMakeLists.txt"
        ).read_text(encoding="utf-8")
        mcuboot_conf = (project / "sysbuild" / "mcuboot.conf").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("zephyr_compile_options", board_cmake)
        self.assertIn("nrf_crypto_keys_housekeeping", mcuboot_conf)

    def test_build_emits_external_factory_image(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("mergehex.py", script)
        self.assertIn("factoryHex = $factoryHex", script)
        self.assertIn("stm32\\zephyr\\zephyr.signed.hex", script)

    def test_board_pin_map_matches_final_netlist(self):
        dts = (
            TOOLS_DIR.parent
            / "boards"
            / "bciband_h563vg"
            / "bciband_h563vg.dts"
        ).read_text(encoding="utf-8")

        for token in (
            "work-led-gpios = <&gpioa 1 GPIO_ACTIVE_HIGH>",
            "ext-trigger-gpios = <&gpiob 7 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>",
            "&sdmmc1_d0_pc8",
            "&sdmmc1_d1_pc9",
            "&sdmmc1_d2_pc10",
            "&sdmmc1_d3_pc11",
            "&sdmmc1_ck_pc12",
            "&sdmmc1_cmd_pd2",
            'disk-name = "SD"',
            "bus-width = <4>",
        ):
            self.assertIn(token, dts)

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
