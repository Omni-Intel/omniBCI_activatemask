import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

import onmibci_gui.window as window_module
from onmibci_gui.common import set_runtime_sample_rate
from onmibci_gui.acquisition import AcquisitionMixin
from onmibci_gui.channel_config import ChannelConfigMixin
from onmibci_gui.display import DisplayMixin
from onmibci_gui.exports import ExportMixin
from onmibci_gui.transport_control import TransportControlMixin


class GuiArchitectureTests(unittest.TestCase):
    def test_serial_firmware_identity_packet_decodes_without_guessing(self):
        packet = bytearray((0xBC, 0xAB, 0x03, 20, 0, 0, 1, 0x6F, 0x00, 1, 0xFF, 0))
        for value in packet[:11]:
            packet[11] ^= value
        decoded = ChannelConfigMixin._decode_config_ack_packet(packet, 0xAB)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["packet"][2:9], bytes((0x03, 20, 0, 0, 1, 0x6F, 0x00)))

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
            self.assertTrue(np.all(window.channel_enabled))
            self.assertTrue(np.all(window.channel_bias))
        finally:
            if window is not None:
                window.close()
            app.processEvents()
            window_module.BLE_AVAILABLE = previous_ble_available

    def test_mcu_selector_separates_stm32_and_esp32_transports(self):
        previous_ble_available = window_module.BLE_AVAILABLE
        window_module.BLE_AVAILABLE = False
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = None
        try:
            window = window_module.MainWindow()
            stm32_index = window.mcu_combo.findData(window_module.MCU_STM32)
            esp32_index = window.mcu_combo.findData(window_module.MCU_ESP32)
            self.assertGreaterEqual(stm32_index, 0)
            self.assertGreaterEqual(esp32_index, 0)

            window.mcu_combo.setCurrentIndex(stm32_index)
            self.assertEqual(window.selected_mcu(), window_module.MCU_STM32)
            self.assertEqual(window.selected_transport(), "serial")
            self.assertFalse(window.transport_combo.isEnabled())

            window.mcu_combo.setCurrentIndex(esp32_index)
            self.assertEqual(window.selected_mcu(), window_module.MCU_ESP32)
            self.assertTrue(window.transport_combo.isEnabled())

            window.set_firmware_identity((20, 0, 0), 1, window_module.BLE_CAP_FULL_DIFF)
            self.assertEqual(window.firmware_profile, "full_diff")
            self.assertIn("V20.0.0", window.firmware_label.text())
            self.assertIn("SRB1/SRB2 OFF", window.reference_fixed_label.text())
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

    def test_live_psd_supports_1000_sps_and_screen_envelope_is_bounded(self):
        previous_ble_available = window_module.BLE_AVAILABLE
        window_module.BLE_AVAILABLE = False
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = None
        try:
            set_runtime_sample_rate(1000)
            window = window_module.MainWindow()
            samples = window_module.FS * 6
            t = np.arange(samples, dtype=float) / window_module.FS
            values = np.vstack(
                [20.0 * np.sin(2.0 * np.pi * (8.0 + ch) * t) for ch in range(8)]
            )
            valid = np.ones(samples, dtype=bool)
            sequence = np.arange(samples, dtype=np.uint32)
            modes = np.zeros(samples, dtype=np.uint8)
            window.ring.append_batch(values, valid, sequence, modes)
            window.active_transport = "serial"
            window.streaming = True

            window.update_psd_and_info()
            deadline = time.monotonic() + 3.0
            while window.psd_worker_busy and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

            self.assertFalse(window.psd_worker_busy)
            frequencies, powers = window.psd_curve.getData()
            self.assertGreater(len(frequencies), 0)
            self.assertEqual(len(frequencies), len(powers))

            plot_t, plot_y = window._live_plot_envelope(t, values[0])
            self.assertLessEqual(plot_t.size, window_module.LIVE_PLOT_MAX_POINTS)
            self.assertEqual(plot_t.size, plot_y.size)
            self.assertGreaterEqual(np.nanmax(plot_y), 19.0)
            self.assertLessEqual(np.nanmin(plot_y), -19.0)
        finally:
            if window is not None:
                window.close()
            app.processEvents()
            set_runtime_sample_rate(250)
            window_module.BLE_AVAILABLE = previous_ble_available


if __name__ == "__main__":
    unittest.main()
