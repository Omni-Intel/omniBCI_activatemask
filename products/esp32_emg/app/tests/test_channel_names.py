import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyedflib

import ads1299_eeg_gui_native as legacy
from ads1299_eeg_gui_native import MainWindow
from onmibci_stream import DEFAULT_CHANNELS, LocalStreamServer
import onmibci_sdk as internal_sdk
from public_sdk import omnibci_sdk as public_sdk


class ChannelNameTests(unittest.TestCase):
    def make_window(self, window_type, transport="serial"):
        window = types.SimpleNamespace(
            sample_rate=250, channel_names=list(DEFAULT_CHANNELS),
            channel_enabled=np.ones(8, dtype=bool),
            channel_gains=np.full(8, 24),
            channel_bias=np.ones(8, dtype=bool),
            channel_srb2=np.zeros(8, dtype=bool),
            impedance_active=False, streaming=True, offline_uv=None,
            active_transport=transport, ble_worker=None,
            stream_server=LocalStreamServer(port=0), stream_api_errors=0,
            ring=mock.Mock(), app_settings=mock.Mock(),
            transport_connected=lambda: True,
            transport_write=mock.Mock(), transport_reset_input_buffer=mock.Mock(),
            stop_impedance_detection=mock.Mock(), reset_processing_state=mock.Mock(),
            set_bias_checks=mock.Mock(), set_status=mock.Mock(),
            bias_register_name=lambda: "BIAS_SENSP",
            last_seq=123, first_seq=10, first_clock=20,
        )
        for name in ("validated_channel_name", "refresh_channel_parameter_labels",
                     "apply_channel_settings", "_publish_stream_batch"):
            setattr(window, name, types.MethodType(getattr(window_type, name), window))
        window.read_config_ack = mock.Mock(return_value={
            "argument": 0, "verified": True, "bias_p": 255, "bias_n": 0,
        })
        window._ble_write_bulk_config = mock.Mock(return_value={"bias_p": 255, "bias_n": 0})
        return window

    def test_name_only_never_touches_hardware_or_acquisition(self):
        for window_type in (MainWindow,):
            for transport in ("serial", "ble"):
                with self.subTest(gui=window_type, transport=transport):
                    window = self.make_window(window_type, transport)
                    window.impedance_active = True
                    window.apply_channel_settings(0, True, 24, True, channel_name="Fp1")
                    window.transport_write.assert_not_called()
                    window._ble_write_bulk_config.assert_not_called()
                    window.stop_impedance_detection.assert_not_called()
                    window.ring.clear.assert_not_called()
                    window.reset_processing_state.assert_not_called()
                    window.app_settings.setValue.assert_not_called()
                    self.assertTrue(window.streaming)
                    self.assertEqual(window.last_seq, 123)
                    self.assertEqual(window.channel_names[0], "Fp1")
                    self.assertEqual(window.stream_server._hello("raw")["channels"][0], "Fp1")
                    window.transport_reset_input_buffer.assert_not_called()

    def test_invalid_names_are_rejected_before_any_device_action(self):
        for window_type in (MainWindow,):
            for name in ("", "ch2", "\u4e2d\u6587", "x" * 17, "bad\nname"):
                with self.subTest(gui=window_type, name=name):
                    window = self.make_window(window_type)
                    with self.assertRaises(ValueError):
                        window.apply_channel_settings(0, True, 24, True, channel_name=name)
                    window.transport_write.assert_not_called()
                    self.assertEqual(window.channel_names, list(DEFAULT_CHANNELS))

    def test_saving_unchanged_settings_does_not_reconfigure(self):
        for window_type in (MainWindow,):
            window = self.make_window(window_type)
            window.apply_channel_settings(0, True, 24, True)
            window.transport_write.assert_not_called()
            window.reset_processing_state.assert_not_called()

    def test_bdf_labels_follow_software_only_rename(self):
        for window_type in (MainWindow,):
            with self.subTest(gui=window_type), tempfile.TemporaryDirectory() as directory:
                window = self.make_window(window_type)
                window.apply_channel_settings(0, True, 24, True, channel_name="Fp1")
                window.offline_uv = np.zeros((8, 250))
                window.offline_valid = np.ones(250, dtype=bool)
                window._write_bdf_data = types.MethodType(window_type._write_bdf_data, window)
                path = Path(directory) / "named.bdf"
                window_type.save_bdf(window, path)
                with pyedflib.EdfReader(str(path)) as reader:
                    self.assertEqual(reader.getSignalLabels(), window.channel_names)

    def test_gain_change_still_writes_and_verifies_registers(self):
        for window_type in (MainWindow,):
            with self.subTest(gui=window_type):
                window = self.make_window(window_type)
                with mock.patch.object(legacy.time, "sleep"):
                    window.apply_channel_settings(0, True, 12, True, channel_name="Fp1")
                window.transport_write.assert_any_call(bytes([0xA7, 0, 12, 3]))
                window.read_config_ack.assert_called_once_with(0xA7, expected_argument=0)
                self.assertEqual(window.channel_gains[0], 12)
                self.assertEqual(window.channel_names[0], "Fp1")

    def test_publish_carries_names_without_changing_samples(self):
        for window_type in (MainWindow,):
            for stream in ("raw", "filtered"):
                with self.subTest(gui=window_type, stream=stream):
                    window = self.make_window(window_type)
                    window.channel_names[0] = "Fp1"
                    window.stream_server.publish = mock.Mock()
                    values = np.arange(16).reshape(8, 2)
                    window._publish_stream_batch(stream, values, np.ones(2, dtype=bool),
                                              np.array([10, 11]), np.zeros(2, dtype=int), None)
                    batch = window.stream_server.publish.call_args.args[0]
                    self.assertEqual(batch.channels, tuple(window.channel_names))
                    np.testing.assert_array_equal(batch.values, values.T)

    def test_both_sdks_receive_live_renames_without_reconnecting(self):
        for sdk in (internal_sdk, public_sdk):
            for stream in ("raw", "filtered"):
                with self.subTest(sdk=sdk.__name__, stream=stream):
                    window = self.make_window(MainWindow)
                    window.channel_names[0] = "Fp1"
                    window.refresh_channel_parameter_labels()
                    server = window.stream_server
                    server.start()
                    iterator = None
                    try:
                        client = sdk.connect_local(port=server.port)
                        iterator = getattr(client, "stream_" + stream)()
                        self.assertEqual(client.hello["channels"][0], "Fp1")
                        values = np.arange(16).reshape(8, 2)
                        window.channel_names[0] = "Cz"
                        window.refresh_channel_parameter_labels()
                        window._publish_stream_batch(stream, values, np.ones(2, dtype=bool),
                                                  np.array([10, 11]), np.zeros(2, dtype=int), None)
                        batch = next(iterator)
                        self.assertEqual(batch.channels[0], "Cz")
                        np.testing.assert_array_equal(batch.values, values.T)
                    finally:
                        if iterator is not None:
                            iterator.close()
                        server.stop()

    def test_sdk_hello_still_rejects_malformed_names(self):
        for sdk in (internal_sdk, public_sdk):
            server = LocalStreamServer(port=0)
            server.start()
            try:
                for channels in ("ABCDEFGH", [], ["Fp1"] * 8,
                                 ["", *DEFAULT_CHANNELS[1:]],
                                 ["\u4e2d\u6587", *DEFAULT_CHANNELS[1:]],
                                 [1, *DEFAULT_CHANNELS[1:]]):
                    with self.subTest(sdk=sdk.__name__, channels=channels):
                        hello = {**server._hello("raw"), "channels": channels}
                        with mock.patch.object(server, "_hello", return_value=hello), self.assertRaises(sdk.ProtocolError):
                            sdk.connect_local(port=server.port).stream_raw()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
