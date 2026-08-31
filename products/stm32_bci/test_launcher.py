"""Run with Python 3.12 and the archived app's dependencies; no hardware needed."""

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.dont_write_bytecode = True


def check_launcher(screenshot=None):
    product = Path(__file__).resolve().parent
    assert (product / "run_stm32.py").is_file(), "STM32 isolation wrapper is missing"
    import run_stm32 as launcher
    from PySide6 import QtCore, QtGui, QtWidgets

    original_settings = QtCore.QSettings
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="stm32-wrapper-check-") as folder:
        root = Path(folder).resolve()
        shutil.copytree(product / "app", root / "app", ignore=shutil.ignore_patterns(
            "__pycache__", ".venv", ".ruff_cache", "logs", "recordings", "*.ini",
        ))
        launcher.APP_DIR = root / "app"
        launcher.LOCK_PATH = root / launcher.LOCK_PATH.name
        settings_file = launcher.APP_DIR / "stm32_bci.ini"
        seeded = QtCore.QSettings(str(settings_file), QtCore.QSettings.IniFormat)
        seeded.setValue("mcu_family", "esp32")
        seeded.setValue("channel_names", ["preserved"])
        seeded.sync()

        try:
            app_module = launcher.prepare_app()
            from onmibci_gui import common, window
            from onmibci_gui.single_instance import SingleInstanceLock
            from onmibci_sdk import LocalClient

            assert QtCore.QSettings is original_settings, "global Qt settings were patched"
            assert Path.cwd() == launcher.APP_DIR
            assert common.APP_DIR == launcher.APP_DIR
            assert common.LOG_DIR == launcher.APP_DIR / "logs"
            assert common.RECORDINGS_DIR == launcher.APP_DIR / "recordings"
            isolated = window.QtCore.QSettings("OmniBCI", "ADS1299EEGWorkbench")
            assert Path(isolated.fileName()) == settings_file
            assert not isolated.fallbacksEnabled()
            assert isolated.value("mcu_family") == "stm32"
            assert isolated.value("channel_names") == ["preserved"]

            first, second = app_module.SingleInstanceLock(), app_module.SingleInstanceLock()
            legacy = SingleInstanceLock(root / "omnibci_v18_gui.lock")
            try:
                assert first.path == launcher.LOCK_PATH
                assert first.acquire(0)
                assert not second.acquire(0), "second STM32 instance was admitted"
                assert legacy.acquire(0), "legacy product lock clashes with STM32"
            finally:
                first.release()
                second.release()
                legacy.release()

            qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            # Windows offscreen Qt does not load the native desktop font database.
            if sys.platform == "win32":
                fonts = Path(os.environ["WINDIR"]) / "Fonts"
                for name in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc"):
                    QtGui.QFontDatabase.addApplicationFont(str(fonts / name))
                qt_app.setFont(QtGui.QFont("Microsoft YaHei", 9))
            win = app_module.MainWindow()
            try:
                assert win.mcu_family == win.selected_mcu() == "stm32"
                assert win.mcu_combo.count() == 1
                assert win.mcu_combo.itemData(0) == "stm32"
                win.mcu_combo.setCurrentIndex(-1)
                win.mcu_mode_changed()
                assert win.selected_mcu() == win.mcu_family == "stm32"
                win.transport_combo.setCurrentIndex(win.transport_combo.findData("ble"))
                assert win.selected_transport() == "serial"
                assert not win.transport_combo.isEnabled()
                assert win.ble_worker is None
                assert win.ser is None and not win.streaming
                assert win.stream_server is not None, "port 8767 must be free for this check"
                assert win.stream_server.port == 8767
                client = LocalClient(url="ws://127.0.0.1:8767/v1/stream")
                assert client.control_url == "ws://127.0.0.1:8767/v1/control"
                with client.stream_raw():
                    assert client.hello["sample_rate"] == 250
                win.mcu_combo.setCurrentIndex(0)
                win.transport_combo.setCurrentIndex(0)
                win.show()
                qt_app.processEvents()
                capture = win.grab()
                assert not capture.isNull()
                if screenshot is not None:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    assert capture.save(str(screenshot)), "could not save GUI screenshot"
            finally:
                win.close()
                qt_app.processEvents()
        finally:
            os.chdir(original_cwd)
    print("PASS: STM32 settings, lock, fixed family/serial, offscreen GUI and SDK port 8767")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=lambda value: Path(value).resolve())
    check_launcher(parser.parse_args().screenshot)
