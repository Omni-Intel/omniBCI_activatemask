"""Offline checks for the stable EEG release entry and artifact policy."""

import os
from pathlib import Path
import runpy
import subprocess
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def spec_analysis(self, exports):
        # Execute the spec without building DLLs; capture PyInstaller's inputs.
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_submodules = lambda name: [name]
        hooks.collect_data_files = lambda name: [(name + "/resource", name)]
        captured = {}

        def analysis(scripts, **kwargs):
            captured.update(scripts=scripts, **kwargs)
            return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        with patch.dict(os.environ, {"OMNIBCI_BUNDLE_EXPORTS": exports}), patch.dict(
            "sys.modules", {"PyInstaller.utils.hooks": hooks}
        ):
            runpy.run_path(str(ROOT / "OmniBCI_V19.spec"), init_globals={
                "Analysis": analysis, "PYZ": lambda *a, **k: None,
                "EXE": lambda *a, **k: None, "COLLECT": lambda *a, **k: None,
            })
        return captured

    def test_packages_the_validated_native_entry(self):
        self.assertEqual(self.spec_analysis("1")["scripts"], ["ads1299_eeg_gui_native.py"])

    def test_full_and_slim_exports(self):
        for module in ("mne", "pyedflib"):
            self.assertNotIn(module, self.spec_analysis("1")["excludes"])
            self.assertIn(module, self.spec_analysis("0")["excludes"])

    def test_mne_lazy_modules_and_resources_are_collected(self):
        full = self.spec_analysis("1")
        self.assertIn("mne", full["hiddenimports"])
        self.assertIn(("mne/resource", "mne"), full["datas"])
        self.assertNotIn("mne", self.spec_analysis("0")["hiddenimports"])

    def test_builder_preserves_runtime_environment_and_old_releases(self):
        script = (ROOT / "build_exe.bat").read_text(encoding="utf-8").lower()
        self.assertNotIn("rmdir", script)
        self.assertIn("uv_project_environment", script)
        self.assertIn("--frozen", script)
        self.assertIn("--distpath", script)

    def test_builder_does_not_collect_dlls_from_unrelated_tools_on_path(self):
        script = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
        clean_path = 'set "PATH=%SystemRoot%\\System32;%SystemRoot%"'
        self.assertIn(clean_path, script)
        self.assertLess(script.index(clean_path), script.index(' -m PyInstaller'))

    def test_desktop_artifacts_ignored_but_firmware_updates_allowed(self):
        for path in ("OmniBCI.exe", "OmniBCI_Windows.zip", "release/GUI.ZIP",
                     "products/esp32_emg/GUI.7z", ".buildvenv/pyvenv.cfg"):
            result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, path)
        path = "products/stm32_bci/firmware/STM32_E73_DONGLE_V19/release/update.zip"
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
        self.assertEqual(result.returncode, 1, path)

    def test_workflow_does_not_upload_desktop_artifacts(self):
        workflow = (ROOT / ".github/workflows/build-v16-windows-exe.yml").read_text(encoding="utf-8")
        self.assertNotIn("actions/upload-artifact", workflow)


if __name__ == "__main__":
    unittest.main()
