import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

import onmibci_gui.window as window_module
from onmibci_gui.acquisition import AcquisitionMixin
from onmibci_gui.channel_config import ChannelConfigMixin
from onmibci_gui.display import DisplayMixin
from onmibci_gui.exports import ExportMixin
from onmibci_gui.transport_control import TransportControlMixin


class GuiArchitectureTests(unittest.TestCase):
    def test_main_window_composes_responsibility_mixins(self):
        expected = {
            ChannelConfigMixin,
            ExportMixin,
            TransportControlMixin,
            AcquisitionMixin,
            DisplayMixin,
        }

        self.assertTrue(expected.issubset(set(window_module.MainWindow.__mro__)))

    def test_main_window_constructs_and_closes_offscreen(self):
        previous_ble_available = window_module.BLE_AVAILABLE
        window_module.BLE_AVAILABLE = False
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = None
        try:
            window = window_module.MainWindow()
            self.assertIn("ADS1299", window.windowTitle())
        finally:
            if window is not None:
                window.close()
            app.processEvents()
            window_module.BLE_AVAILABLE = previous_ble_available

    def test_live_psd_result_reaches_gui_and_updates_curve(self):
        previous_ble_available = window_module.BLE_AVAILABLE
        window_module.BLE_AVAILABLE = False
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = None
        try:
            window = window_module.MainWindow()
            samples = window_module.FS * 6
            rng = np.random.default_rng(18)
            values = rng.normal(0.0, 5.0, size=(window_module.CHANNELS, samples))
            valid = np.ones(samples, dtype=bool)
            sequence = np.arange(samples, dtype=np.uint32)
            modes = np.zeros(samples, dtype=np.uint8)
            window.ring.append_batch(values, valid, sequence, modes)
            # Regression: the old BLE-oriented self-repair flag leaked into
            # serial sessions and held PSD at "waiting for four seconds".
            window.active_transport = "serial"
            window.streaming = True
            window._self_repair_active = True
            window._self_repair_until = time.monotonic() + 30.0

            window.update_psd_and_info()
            self.assertTrue(window.psd_worker_busy)
            self.assertIsNotNone(window.psd_active_worker)
            deadline = time.monotonic() + 2.0
            while window.psd_worker_busy and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

            self.assertFalse(window.psd_worker_busy)
            self.assertIsNone(window.psd_active_worker)
            frequencies, powers = window.psd_curve.getData()
            self.assertGreater(len(frequencies), 0)
            self.assertEqual(len(frequencies), len(powers))
            window._observe_render_gap(250.0, time.monotonic())
            self.assertFalse(window._self_repair_active)
        finally:
            if window is not None:
                window.close()
            app.processEvents()
            window_module.BLE_AVAILABLE = previous_ble_available


if __name__ == "__main__":
    unittest.main()
