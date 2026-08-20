import asyncio
import struct
import unittest

from onmibci_gui.common import BLE_BLOCK_MAGIC, BLE_BLOCK_VERSION_V2, BLE_CTRL_ACK
from onmibci_gui.frames import crc16_ccitt
from onmibci_gui.transports import BleTransportWorker


class _FakeBleClient:
    is_connected = True

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, characteristic, payload, response=False):
        self.writes.append((characteristic, bytes(payload), bool(response)))


class BleV4TimingTests(unittest.TestCase):
    def test_v4_uses_shallow_ack_window_without_changing_v5(self):
        worker = BleTransportWorker()

        worker.set_peer_status_protocol(4)
        legacy = worker.adaptive_timing()
        self.assertEqual(legacy["profile"], "legacy-v4-rescue")
        self.assertEqual(legacy["ack_every_blocks"], 3)
        self.assertLessEqual(legacy["ack_interval_s"], 0.06)

        worker.set_peer_status_protocol(5)
        current = worker.adaptive_timing()
        self.assertNotEqual(current["profile"], "legacy-v4-rescue")
        self.assertEqual(current["ack_every_blocks"], 3)

    @staticmethod
    def _gap_block(session_id, block_sequence):
        body = (
            BLE_BLOCK_MAGIC
            + bytes((BLE_BLOCK_VERSION_V2, 0x04))
            + struct.pack("<III", session_id, block_sequence, 0)
            + bytes((0, 0))
            + struct.pack("<H", 0)
        )
        return body + struct.pack("<H", crc16_ccitt(body))

    def test_v4_fast_forwards_phantom_missing_block_without_nack(self):
        worker = BleTransportWorker()
        worker.set_peer_status_protocol(4)
        wire = self._gap_block(9, 0) + self._gap_block(9, 2)

        _payloads, controls = worker._decode_reliable_bytes_locked(wire)
        metrics = worker.reliable_metrics()

        self.assertEqual(metrics["expected_block"], 3)
        self.assertEqual(metrics["legacy_v4_fast_forward_blocks"], 1)
        self.assertFalse(any(kind == "nack" for _packet, kind in controls))

    def test_v5_keeps_lossless_nack_repair(self):
        worker = BleTransportWorker()
        worker.set_peer_status_protocol(5)
        wire = self._gap_block(9, 0) + self._gap_block(9, 2)

        _payloads, controls = worker._decode_reliable_bytes_locked(wire)
        metrics = worker.reliable_metrics()

        self.assertEqual(metrics["expected_block"], 1)
        self.assertEqual(metrics["pending_blocks"], 1)
        self.assertTrue(any(kind == "nack" for _packet, kind in controls))


class BleV4ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = BleTransportWorker()
        self.worker._client = _FakeBleClient()
        self.worker._gatt_write_lock = asyncio.Lock()
        self.worker._reliable_session_id = 7
        self.worker._reliable_last_ack_wire = 5

    async def test_retry_repeats_ack_that_normal_dedup_suppresses(self):
        packet = self.worker._make_reliable_control_packet(BLE_CTRL_ACK, 7, 5)

        await self.worker._send_reliable_control(packet, "ack")
        self.assertEqual(self.worker._client.writes, [])

        await self.worker._send_reliable_control(packet, "ack_retry")
        self.assertEqual(len(self.worker._client.writes), 1)
        self.assertFalse(self.worker._client.writes[-1][2])
        self.assertEqual(self.worker.reliable_metrics()["legacy_v4_ack_retries"], 1)

    async def test_reset_avoids_blocking_gatt_response_queue(self):
        self.worker.set_peer_status_protocol(4)
        packet = self.worker._make_reliable_control_packet(BLE_CTRL_ACK, 7, 5)

        await self.worker._send_reliable_control(packet, "reset")

        self.assertEqual(len(self.worker._client.writes), 1)
        self.assertFalse(self.worker._client.writes[-1][2])

    async def test_v5_reset_keeps_write_response_confirmation(self):
        self.worker.set_peer_status_protocol(5)
        packet = self.worker._make_reliable_control_packet(BLE_CTRL_ACK, 7, 5)

        await self.worker._send_reliable_control(packet, "reset")

        self.assertEqual(len(self.worker._client.writes), 1)
        self.assertTrue(self.worker._client.writes[-1][2])


if __name__ == "__main__":
    unittest.main()
