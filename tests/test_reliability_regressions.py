import struct
import asyncio
import tempfile
import types
from unittest import mock
import unittest
from pathlib import Path

import ads1299_eeg_gui_native as gui


def make_reliable_gap_marker(session_id: int, block_sequence: int) -> bytes:
    packet = bytearray(gui.BLE_BLOCK_HEADER_BYTES + gui.BLE_BLOCK_CRC_BYTES)
    packet[0:2] = gui.BLE_BLOCK_MAGIC
    packet[2] = gui.BLE_BLOCK_VERSION_V2
    packet[3] = 0x04
    struct.pack_into("<I", packet, 4, session_id)
    struct.pack_into("<I", packet, 8, block_sequence)
    struct.pack_into("<H", packet, 18, 0)
    struct.pack_into("<H", packet, len(packet) - 2, gui.crc16_ccitt(packet[:-2]))
    return bytes(packet)


class ReliabilityRegressionTests(unittest.TestCase):
    def test_new_ble_session_resets_wire_ack_watermark(self):
        worker = gui.BleTransportWorker()
        worker._reliable_session_id = 10
        worker._reliable_accept_any_session = True
        worker._reliable_last_ack_wire = 123

        worker._decode_reliable_bytes_locked(make_reliable_gap_marker(11, 0))

        self.assertEqual(worker._reliable_session_id, 11)
        self.assertEqual(worker._reliable_last_ack_wire, 0xFFFFFFFF)

    def test_missing_prefix_stops_before_first_pending_block(self):
        self.assertEqual(gui.reliable_missing_prefix(100, [105, 106, 200], 63), (100, 104))
        self.assertIsNone(gui.reliable_missing_prefix(100, [100, 105], 63))

    def test_decoder_nack_stops_before_first_pending_block(self):
        worker = gui.BleTransportWorker()
        worker._reliable_session_id = 7
        worker._reliable_accept_any_session = False
        worker._reliable_expected_block = 100
        worker._decode_reliable_bytes_locked(make_reliable_gap_marker(7, 105))
        worker._reliable_last_nack = None

        _payloads, controls = worker._decode_reliable_bytes_locked(
            make_reliable_gap_marker(7, 110)
        )

        nack = next(packet for packet, kind in controls if kind == "nack")
        self.assertEqual(struct.unpack_from("<II", nack, 8), (100, 104))

    def test_ack_keepalive_bypasses_duplicate_suppression(self):
        class FakeClient:
            is_connected = True

            def __init__(self):
                self.writes = []

            async def write_gatt_char(self, uuid, packet, response):
                self.writes.append((uuid, packet, response))

        async def run_test():
            worker = gui.BleTransportWorker()
            worker._client = FakeClient()
            worker._gatt_write_lock = asyncio.Lock()
            worker._reliable_session_id = 9
            worker._reliable_last_ack_wire = 12
            packet = worker._make_reliable_control_packet(gui.BLE_CTRL_ACK, 9, 12)

            await worker._send_reliable_control(packet, "ack_keepalive")

            self.assertEqual(len(worker._client.writes), 1)
            self.assertFalse(worker._client.writes[0][2])
            self.assertEqual(worker._reliable_ack_sent, 1)

        asyncio.run(run_test())

    def test_saturation_percent_uses_actual_eligible_samples(self):
        self.assertEqual(gui.saturation_percent(4, 2000), 0.2)
        self.assertEqual(gui.saturation_percent(4, 0), 0.0)

    def test_writer_refuses_new_session_while_old_thread_is_alive(self):
        class AliveThread:
            @staticmethod
            def is_alive():
                return True

        writer = gui.AsyncRawWriter()
        writer._thread = AliveThread()
        writer.stop = lambda timeout=2.0: None

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "未退出"):
                writer.start_session(str(Path(directory)))

    def test_ble_bulk_config_rejects_gain_readback_mismatch(self):
        window = gui.MainWindow.__new__(gui.MainWindow)
        window.ble_worker = types.SimpleNamespace(
            request_blocking=lambda *args, **kwargs: b"snapshot",
            config_snapshot=None,
        )
        window.channel_enabled = [True] * gui.CHANNELS
        window.channel_bias = [True] * gui.CHANNELS
        window.channel_gains = [24] * gui.CHANNELS
        window.current_mode = 1
        window.impedance_active = False
        window.impedance_mask = 0
        snapshot = types.SimpleNamespace(
            verified=True,
            enabled_mask=0xFF,
            bias_p=0xFF,
            bias_n=0,
            mode=1,
            generation=1,
            channel_registers=(0x50,) + (0x60,) * 7,
        )

        with mock.patch.object(gui, "decode_config_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(RuntimeError, "读回不一致"):
                window._ble_write_bulk_config(gui.REFERENCE_SRB1)

    def test_ble_diagnostic_mode_accepts_zero_bias_readback(self):
        window = gui.MainWindow.__new__(gui.MainWindow)
        window.ble_worker = types.SimpleNamespace(
            request_blocking=lambda *args, **kwargs: b"snapshot",
            config_snapshot=None,
        )
        window.channel_enabled = [True] * gui.CHANNELS
        window.channel_bias = [True] * gui.CHANNELS
        window.channel_gains = [24] * gui.CHANNELS
        window.current_mode = 3
        window.impedance_active = False
        window.impedance_mask = 0
        snapshot = types.SimpleNamespace(
            verified=True,
            enabled_mask=0xFF,
            bias_p=0,
            bias_n=0,
            mode=3,
            generation=1,
            channel_registers=(0x61,) * 8,
        )

        with mock.patch.object(gui, "decode_config_snapshot", return_value=snapshot):
            result = window._ble_write_bulk_config(gui.REFERENCE_SRB1)

        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
