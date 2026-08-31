"""Run with python -m unittest tests.test_emg_product (no hardware)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtCore, QtWidgets
import ads1299_eeg_gui_native as gui
import onmibci_ble_protocol as protocol
from onmibci_stream import LocalStreamServer
from omnibci.recording import AsyncRawWriter
import onmibci_sdk
from public_sdk import omnibci_sdk


def snapshot(rate=250, mode=1, mask=255):
    code = (250, 500, 1000).index(rate)
    data = bytearray(29)
    data[5:13] = bytes((mode, 1, mask, mask, 0, 0x96-code, 0xC0, 0xEC))
    data[13:21] = bytes((0x60,)) * 8
    data[21:23] = bytes((mask, mask)) if mode in (0, 1) else bytes(2)
    data[26] = code
    data[27:29] = rate.to_bytes(2, "little")
    return bytes(data)


def rate_ack(rate):
    code = (250, 500, 1000).index(rate)
    packet = bytearray((0xBC, 0xAA, code, 0x96-code, rate & 255,
                        rate >> 8, code, 0, 1, 1, 255, 0))
    for value in packet[:11]:
        packet[11] ^= value
    return bytes(packet)


class EmgProtocolTests(unittest.TestCase):
    def test_snapshot_rates_and_invalid_readback(self):
        for rate in (250, 500, 1000):
            decoded = protocol.decode_config_snapshot(snapshot(rate))
            self.assertEqual(decoded.sample_rate_hz, rate)
            self.assertEqual(decoded.config1, 0x96-(250, 500, 1000).index(rate))
        for offset, value in ((0, 1), (6, 0), (10, 0x94), (26, 4), (27, 0)):
            invalid = bytearray(snapshot())
            invalid[offset] = value
            with self.subTest(offset=offset), self.assertRaises(protocol.ProtocolError):
                protocol.decode_config_snapshot(invalid)

    def test_usb_ack_rate_fields(self):
        for rate in (250, 500, 1000):
            ack = gui.MainWindow._decode_config_ack_packet(rate_ack(rate), 0xAA)
            self.assertEqual(ack.get("sample_rate_hz"), rate)
            broken = bytearray(rate_ack(rate))
            broken[11] ^= 1
            self.assertIsNone(gui.MainWindow._decode_config_ack_packet(broken, 0xAA))

    def test_writer_rotates_at_actual_minute(self):
        for rate in (250, 500, 1000):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as folder:
                writer = AsyncRawWriter()
                writer.start_session(folder, {"sample_rate_hz": rate})
                try:
                    self.assertTrue(writer.submit(bytes(48 * (rate * 60 + 1))))
                finally:
                    writer.stop()
                state = writer.snapshot()
                self.assertEqual(state["segments"][0]["bytes"], 48 * rate * 60)
                self.assertEqual(state["segments"][0]["duration_seconds"], 60.0)
                meta = json.loads(Path(state["manifest_path"]).read_text(encoding="utf-8"))
                self.assertEqual(meta["configuration_at_session_start"]["sample_rate_hz"], rate)


class EmgWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = QtCore.QSettings(str(Path(self.temp.name) / "test.ini"), QtCore.QSettings.IniFormat)
        for patcher in (mock.patch.object(gui, "BLE_AVAILABLE", False),
                        mock.patch.object(gui.MainWindow, "refresh_ports"),
                        mock.patch.object(gui.LocalStreamServer, "start"),
                        mock.patch.object(gui.QtCore, "QSettings", return_value=settings)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = gui.MainWindow()
        self.addCleanup(self.window.close)

    def set_rate(self, rate):
        self.assertTrue(callable(getattr(self.window, "set_sample_rate_local", None)),
                        "native GUI needs per-window sample-rate propagation")
        self.window.set_sample_rate_local(rate)

    def test_sample_rate_controls_fit_initial_window(self):
        w = self.window
        w.show()
        self.app.processEvents()
        for widget in (w.sample_rate_combo, w.sample_rate_apply_btn):
            bounds = QtCore.QRect(widget.mapTo(w, QtCore.QPoint()), widget.size())
            self.assertTrue(w.rect().contains(bounds), f"sample-rate control outside window: {bounds}")
            self.assertFalse(widget.visibleRegion().isEmpty(), "sample-rate control hidden by toolbar overflow")

    def test_rate_propagates_to_filters_axes_metadata_and_streams(self):
        w = self.window
        for rate in (250, 500, 1000):
            self.set_rate(rate)
            self.assertEqual(w._recording_configuration_snapshot()["sample_rate_hz"], rate)
            self.assertEqual(w.stream_server._hello("raw")["sample_rate"], rate)
            self.assertEqual(w.psd_max_spin.maximum(), rate / 2)
            np.testing.assert_allclose(w.sos_display_band,
                gui.signal.butter(2, [w.hp_spin.value(), w.lp_spin.value()],
                                  btype="bandpass", fs=rate, output="sos"))
            for stream in ("raw", "filtered"):
                with mock.patch.object(w.stream_server, "publish") as publish:
                    w._publish_stream_batch(stream, np.zeros((8, 10)), np.ones(10, bool),
                                            np.arange(10, dtype=np.uint32), np.ones(10, np.uint8), 0)
                    self.assertEqual(publish.call_args.args[0].sample_rate, rate)

    def test_full_diff_mode_one_and_name_only_control(self):
        w = self.window
        w.current_mode = 1
        self.assertEqual(w.bias_register_name(), "BIAS_SENSP+BIAS_SENSN")
        w.set_reference_mode_local(gui.REFERENCE_SRB2)
        self.assertFalse(w.reference_is_srb2())
        self.assertEqual(w._recording_configuration_snapshot()["reference"], "FULL_DIFF")
        with mock.patch.object(w, "transport_write") as write:
            w.apply_channel_settings(0, bool(w.channel_enabled[0]), int(w.channel_gains[0]),
                                     bool(w.channel_bias[0]), channel_name="EMG1")
            write.assert_not_called()
        self.assertEqual(w.stream_server._hello("raw")["channels"][0], "EMG1")

    def test_ble_rate_transaction_and_bad_ack_do_not_change_local_rate(self):
        w = self.window
        self.assertTrue(callable(getattr(w, "configure_sample_rate", None)), "rate control missing")
        w.active_transport, w.ble_connected = "ble", True
        worker = mock.Mock()
        w.ble_worker = worker
        for rate in (250, 500, 1000):
            worker.request_blocking.return_value = snapshot(rate)
            w.configure_sample_rate(rate)
            worker.request_blocking.assert_called_with(0x06, bytes(((250,500,1000).index(rate),)), timeout=4.0)
            self.assertEqual(w.sample_rate, rate)
        worker.request_blocking.return_value = snapshot(250)
        with self.assertRaises((ValueError, RuntimeError)):
            w.configure_sample_rate(500)
        self.assertEqual(w.sample_rate, 1000)
        w.streaming = True
        with self.assertRaises((ValueError, RuntimeError)):
            w.configure_sample_rate(250)
        w.streaming = False
        w.ble_connected = False

    def test_usb_rate_transaction(self):
        w = self.window
        self.assertTrue(callable(getattr(w, "configure_sample_rate", None)), "rate control missing")
        w.active_transport, w.ser = "serial", mock.Mock(is_open=True)
        with mock.patch.object(w, "transport_write") as write, mock.patch.object(w, "read_config_ack") as read:
            for rate in (250, 500, 1000):
                read.return_value = gui.MainWindow._decode_config_ack_packet(rate_ack(rate), 0xAA)
                w.configure_sample_rate(rate)
                write.assert_called_with(bytes((0xAA, (250,500,1000).index(rate))))
                self.assertEqual(w.sample_rate, rate)
            for invalid in (None, {**read.return_value, "verified": False},
                            {**read.return_value, "config1": 0x96}):
                read.return_value = invalid
                with self.assertRaises((ValueError, RuntimeError)):
                    w.configure_sample_rate(500)
                self.assertEqual(w.sample_rate, 1000)

    def test_device_rate_change_does_not_retime_loaded_file(self):
        w = self.window
        data = np.zeros((8, 250))
        w.offline_uv = data
        w.active_transport, w.ble_connected = "ble", True
        w.ble_worker = mock.Mock()
        w.ble_worker.request_blocking.return_value = snapshot(1000)
        w.configure_sample_rate(1000)
        self.assertEqual(w.hardware_sample_rate, 1000)
        self.assertEqual(w.sample_rate, 250)
        self.assertIs(w.offline_uv, data)
        w.apply_ble_config_snapshot(protocol.decode_config_snapshot(snapshot(500)))
        self.assertEqual(w.hardware_sample_rate, 500)
        self.assertEqual(w.sample_rate, 250)
        w.ble_connected = False

    def test_usb_startup_synchronizes_full_channel_state(self):
        w = self.window
        self.assertTrue(callable(getattr(w, "sync_serial_configuration", None)))
        w.active_transport, w.ser = "serial", mock.Mock(is_open=True)
        ack = {"verified": True, "enabled_mask": 255, "bias_p": 255,
               "bias_n": 255, "misc1": 0, "mode": 1}
        with mock.patch.object(w, "transport_write") as write, mock.patch.object(w, "read_config_ack", return_value=ack):
            w.sync_serial_configuration()
            self.assertIn(mock.call(bytes((0xA5, 0, 255, 255, 0, *([24]*8)))), write.call_args_list)

    def test_failed_rate_transaction_requires_reverification(self):
        w = self.window
        w.active_transport, w.ble_connected = "ble", True
        w.ble_worker = mock.Mock()
        w.ble_worker.request_blocking.side_effect = TimeoutError("lost ACK")
        w.hardware_sample_rate = 250
        with self.assertRaises(TimeoutError):
            w.configure_sample_rate(1000)
        self.assertIsNone(w.hardware_sample_rate)
        w.ble_connected = False

    def test_full_diff_impedance_uses_both_lead_off_sides(self):
        w = self.window
        w.active_transport, w.ble_connected = "ble", True
        w.ble_worker = mock.Mock()
        payload = bytearray(snapshot())
        payload[9], payload[23], payload[24] = 1, 1, 1
        w.ble_worker.request_blocking.return_value = bytes(payload)
        with mock.patch.object(w, "selected_impedance_mask", return_value=1), \
             mock.patch.object(w, "transport_write"), mock.patch.object(w, "transport_reset_input_buffer"), \
             mock.patch.object(gui.QtWidgets.QMessageBox, "critical") as critical:
            w.start_impedance_detection()
            self.assertTrue(w.impedance_active, str(critical.call_args))
        w.streaming = w.impedance_active = w.ble_connected = False

    def test_close_stops_timers_and_worker_threads(self):
        w = self.window
        w.close()
        self.assertFalse(any(timer.isActive() for timer in w.findChildren(QtCore.QTimer)))

    def test_lead_off_carrier_is_clock_derived_not_sample_rate_divided(self):
        w = self.window
        for rate in (250, 500, 1000):
            self.set_rate(rate)
            w.impedance_active, w.impedance_mask = True, 1
            w.impedance_value_labels = [QtWidgets.QLabel() for _ in range(8)]
            w.impedance_quality_labels = [QtWidgets.QLabel() for _ in range(8)]
            w.impedance_series_spin = QtWidgets.QDoubleSpinBox()
            t = np.arange(rate * 4) / rate
            values = np.tile(60 * np.sin(2 * np.pi * 31.25 * t), (8, 1))
            w.ring.append_batch(values, np.ones(t.size, bool), np.arange(t.size, dtype=np.uint32), np.ones(t.size, np.uint8))
            w.update_impedance_results()
            self.assertTrue(w.impedance_value_labels[0].text().startswith("10.0"))
            w.impedance_active = False

    def test_bin_sidecar_restores_rate_gain_names(self):
        from tests.test_bdf_export import make_frame
        w = self.window
        path = Path(self.temp.name) / "capture.bin"
        path.write_bytes(make_frame(0, 100) + make_frame(1, 101))
        path.with_suffix(".meta.json").write_text(json.dumps({"configuration": {
            "sample_rate_hz": 1000, "channel_gains": [12]*8,
            "channel_names": [f"EMG{i}" for i in range(1,9)]}}), encoding="utf-8")
        w._load_bin_path(str(path))
        self.assertEqual(w.sample_rate, 1000)
        self.assertEqual(w.channel_names[0], "EMG1")
        self.assertEqual(w.channel_gains[0], 12)

    def test_bad_bin_metadata_does_not_change_current_recording(self):
        from tests.test_bdf_export import make_frame
        w = self.window
        w.offline_uv = np.zeros((8, 250))
        previous_data = w.offline_uv
        path = Path(self.temp.name) / "bad.bin"
        for names, content in ((["short"], make_frame(0, 100)),
                               ([f"EMG{i}" for i in range(8)], b"not a frame")):
            path.write_bytes(content)
            path.with_suffix(".meta.json").write_text(json.dumps({"configuration": {
                "sample_rate_hz": 1000, "channel_gains": [12] * 8, "channel_names": names}}), encoding="utf-8")
            with self.assertRaises((ValueError, RuntimeError)):
                w._load_bin_path(str(path))
            self.assertEqual(w.sample_rate, 250)
            self.assertEqual(w.channel_gains.tolist(), [24] * 8)
            self.assertEqual(w.channel_names, [f"CH{i}" for i in range(1, 9)])
            self.assertIs(w.offline_uv, previous_data)

    def test_live_and_import_gap_limits_follow_sample_rate(self):
        from tests.test_bdf_export import make_frame
        w = self.window
        self.set_rate(1000)
        frames = gui.AdsFrameParser(w.channel_lsb_uv).feed(make_frame(0, 100))
        w.active_transport = "ble"
        with mock.patch.object(gui, "expand_frames_to_timeline", wraps=gui.expand_frames_to_timeline) as expand:
            w.process_frames(frames, live=True)
            self.assertEqual(expand.call_args.kwargs["max_fill_samples"], 30000)
        path = Path(self.temp.name) / "gaps.bin"
        path.write_bytes(make_frame(0, 100))
        path.with_suffix(".meta.json").write_text(json.dumps({"configuration": {"sample_rate_hz": 1000}}), encoding="utf-8")
        with mock.patch.object(gui, "expand_frames_to_timeline", wraps=gui.expand_frames_to_timeline) as expand:
            w._load_bin_path(str(path))
            self.assertEqual(expand.call_args.kwargs["max_fill_samples"], 30000)

    def test_bdf_import_preserves_source_rate(self):
        w = self.window
        self.set_rate(1000)
        w.offline_uv = np.zeros((8, 1000))
        w.offline_valid = np.ones(1000, bool)
        path = Path(self.temp.name) / "source.bdf"
        w.save_bdf(path)
        self.set_rate(250)
        w._load_bdf_path(str(path))
        self.assertEqual(w.sample_rate, 1000)
        self.assertEqual(w.offline_uv.shape[1], 1000)

    def test_recording_export_uses_saved_rate_after_gui_rate_changes(self):
        import pyedflib
        from tests.test_bdf_export import make_frame
        w = self.window
        self.set_rate(1000)
        w.raw_writer.start_session(self.temp.name, w._recording_configuration_snapshot())
        w.raw_writer.submit(b"".join(make_frame(i, 10) for i in range(1000)))
        w.raw_writer.stop()
        self.set_rate(250)
        path = Path(self.temp.name) / "saved-rate.bdf"
        w.export_recording_bdf(path, (), recording_id=w.raw_writer.session_id,
                               recording_started_at=0, first_sequence=0)
        with pyedflib.EdfReader(str(path)) as reader:
            self.assertEqual(reader.getSampleFrequency(0), 1000)

    def test_bdf_and_fif_export_rate_and_labels(self):
        import pyedflib
        import mne
        w = self.window
        for rate in (250, 500, 1000):
            self.set_rate(rate)
            w.offline_uv = np.zeros((8, rate))
            w.offline_valid = np.ones(rate, bool)
            w.channel_names[0] = "EMG1"
            bdf = Path(self.temp.name) / f"{rate}.bdf"
            w.save_bdf(bdf)
            with pyedflib.EdfReader(str(bdf)) as reader:
                self.assertEqual(reader.getSampleFrequency(0), rate)
                self.assertEqual(reader.getSignalLabels()[0], "EMG1")
            fif = Path(self.temp.name) / f"{rate}_raw.fif"
            w.build_mne_raw().save(fif, overwrite=True, verbose="ERROR")
            raw = mne.io.read_raw_fif(fif, verbose="ERROR")
            self.assertEqual(raw.info["sfreq"], rate)
            self.assertEqual(raw.ch_names[0], "EMG1")


class EmgSdkTests(unittest.TestCase):
    def test_both_sdks_accept_supported_hello_rates(self):
        for sdk in (onmibci_sdk, omnibci_sdk):
            for rate in (250, 500, 1000):
                server = LocalStreamServer(port=0)
                server.sample_rate = rate
                server.start()
                try:
                    client = sdk.LocalClient(f"ws://127.0.0.1:{server.port}/v1/stream")
                    stream = client.stream_raw()
                    self.assertEqual(client.hello["sample_rate"], rate)
                    stream.close()
                finally:
                    server.stop()


if __name__ == "__main__":
    unittest.main()
