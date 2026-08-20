"""Tests for the BLE connect handshake: stale-stream recovery and errors."""

import asyncio
from unittest import mock
import types
import unittest

from onmibci_ble_protocol import (
    MSG_GET_CONFIG,
    MSG_HELLO,
    MSG_RESPONSE,
    PROTOCOL_VERSION,
    decode_packet,
    encode_packet,
)

import omnibci.transport_ble as transport_ble
from omnibci.transport_ble import (
    BLE_CONTROL_UUID,
    format_ble_error,
    is_preferred_device_name,
    advertises_omnibci_service,
)


def _config_snapshot_payload(result: int = 0) -> bytes:
    """26-byte GET_CONFIG response: result + generation + registers."""
    return bytes([result]) + (1).to_bytes(4, "little") + bytes(
        [
            1,   # mode
            1,   # verified
            0x1F, 0x1F, 0x00,  # enabled/bias/lead-off masks
            0x96, 0xC0, 0x60,  # CONFIG1..3
        ]
    ) + bytes([0x60] * 8) + bytes([0x1F, 0x00, 0x00, 0x00, 0x20])


class FakeServices:
    def __init__(self, count: int = 5):
        self._count = count

    def get_characteristic(self, uuid):
        return object() if self._count > 0 else None

    def __len__(self):
        return self._count


class FakeBleClient:
    """Minimal BleakClient double answering the device-control protocol."""

    def __init__(self, device, disconnected_callback=None, timeout=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.mtu_size = 247
        self.services = FakeServices(5)
        self.writes = []
        self.worker = None
        self.get_config_results = []

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        pass

    async def stop_notify(self, uuid):
        pass

    async def read_gatt_char(self, uuid):
        return b""

    async def write_gatt_char(self, uuid, data, response=False):
        data = bytes(data)
        self.writes.append((uuid, data, bool(response)))
        if data[:2] == b"\xBC\x52":
            self._answer_device_packet(decode_packet(data))

    def _answer_device_packet(self, packet):
        if packet.message_type == MSG_HELLO:
            payload = bytes((0, 19, 0, 0, PROTOCOL_VERSION, 0, 0, 1, 0, 0, 0))
        elif packet.message_type == MSG_GET_CONFIG:
            payload = (
                self.get_config_results.pop(0)
                if self.get_config_results
                else _config_snapshot_payload(0)
            )
        else:
            payload = b""
        reply = encode_packet(
            MSG_RESPONSE | packet.message_type, packet.request_id, payload
        )
        self.worker._on_response(None, reply)


class BleConnectHandshakeTests(unittest.TestCase):
    def _make_worker(self, client):
        worker = transport_ble.BleTransportWorker()
        worker._closing = False
        worker._desired_key = "AA:BB:CC:DD:EE:FF"
        worker._client = None
        worker._devices = {
            worker._desired_key: types.SimpleNamespace(
                address=worker._desired_key, name="OmniBCI-test"
            )
        }
        client.worker = worker
        return worker

    def _run_connect(self, worker, client):
        async def connect():
            worker._connect_lock = asyncio.Lock()
            worker._loop = asyncio.get_running_loop()
            await worker._connect_to_device(worker._desired_key, reconnected=False)

        def client_factory(device, **kwargs):
            # _connect_to_device constructs BleakClient(device, ...); hand
            # back the pre-configured fake so writes/answers are observable.
            client.device = device
            return client

        with mock.patch.object(transport_ble, "BleakClient", client_factory):
            asyncio.run(connect())

    def test_format_ble_error_keeps_exception_type_for_empty_messages(self):
        self.assertEqual(format_ble_error(Exception("")), "Exception")
        self.assertEqual(format_ble_error(ValueError("boom")), "ValueError: boom")
        self.assertEqual(
            format_ble_error(OSError(" ")),
            "OSError",
        )

    def test_fresh_connect_recovers_from_stale_streaming_device(self):
        client = FakeBleClient(None)
        # First GET_CONFIG answers BUSY: the device is still streaming the
        # previous session that never received its stop command.
        client.get_config_results = [_config_snapshot_payload(2)]
        worker = self._make_worker(client)

        with mock.patch.object(transport_ble, "BleakClient", FakeBleClient):
            self._run_connect(worker, client)

        # The pre-subscribe stop plus the BUSY-retry stop were both sent.
        stop_writes = [
            w for w in client.writes if w[0] == BLE_CONTROL_UUID and w[1] == b"s"
        ]
        self.assertGreaterEqual(len(stop_writes), 2)
        self.assertIsNotNone(worker.config_snapshot)
        self.assertTrue(worker.config_snapshot.verified)

    def test_fresh_connect_stops_idle_device_once(self):
        client = FakeBleClient(None)
        worker = self._make_worker(client)

        with mock.patch.object(transport_ble, "BleakClient", FakeBleClient):
            self._run_connect(worker, client)

        stop_writes = [
            w for w in client.writes if w[0] == BLE_CONTROL_UUID and w[1] == b"s"
        ]
        self.assertEqual(len(stop_writes), 1)
        self.assertIsNotNone(worker.config_snapshot)

    def test_empty_gatt_database_reports_discovery_failure(self):
        client = FakeBleClient(None)
        client.services = FakeServices(0)
        worker = self._make_worker(client)

        with mock.patch.object(transport_ble, "BleakClient", FakeBleClient):
            with self.assertRaisesRegex(RuntimeError, "GATT 服务发现失败"):
                self._run_connect(worker, client)

    def test_missing_characteristics_reports_wrong_device(self):
        class OneServiceNoCharacteristics(FakeServices):
            def __init__(self):
                super().__init__(1)

            def get_characteristic(self, uuid):
                return None

        client = FakeBleClient(None)
        client.services = OneServiceNoCharacteristics()
        worker = self._make_worker(client)

        with mock.patch.object(transport_ble, "BleakClient", FakeBleClient):
            with self.assertRaisesRegex(RuntimeError, "缺少 OmniBCI BLE 特征"):
                self._run_connect(worker, client)


class BleScanPreferenceTests(unittest.TestCase):
    def test_prefix_match_recognises_truncated_advertisement_name(self):
        # V19.0 firmware advertisement truncates the name to "OmniBCI-".
        self.assertTrue(is_preferred_device_name("OmniBCI-"))
        self.assertTrue(is_preferred_device_name("OmniBCI-C3-SRB1-V19"))
        self.assertTrue(is_preferred_device_name("OmniBCI-C3-ADS1299"))
        self.assertFalse(is_preferred_device_name(""))
        self.assertFalse(is_preferred_device_name("SomePhone"))
        self.assertFalse(is_preferred_device_name("未命名 BLE 设备"))

    def test_advertised_service_uuid_is_recognised_case_insensitively(self):
        service = transport_ble.BLE_SERVICE_UUID
        self.assertTrue(
            advertises_omnibci_service(
                types.SimpleNamespace(metadata={"uuids": [service.upper()]})
            )
        )
        self.assertTrue(
            advertises_omnibci_service(
                types.SimpleNamespace(metadata={"uuids": [service]})
            )
        )
        self.assertFalse(
            advertises_omnibci_service(
                types.SimpleNamespace(metadata={"uuids": ["0000ffe0-..."]})
            )
        )
        self.assertFalse(
            advertises_omnibci_service(types.SimpleNamespace(metadata={}))
        )
        self.assertFalse(
            advertises_omnibci_service(types.SimpleNamespace(metadata=None))
        )
        self.assertFalse(advertises_omnibci_service(types.SimpleNamespace()))

    def test_scan_rows_flag_truncated_name_and_uuid_devices_as_preferred(self):
        devices = [
            types.SimpleNamespace(address="A", name="OmniBCI-", metadata={}),
            types.SimpleNamespace(
                address="B", name="Renamed-Dev", metadata={"uuids": [transport_ble.BLE_SERVICE_UUID]}
            ),
            types.SimpleNamespace(address="C", name="Phone", metadata={}),
        ]

        class FakeScanner:
            @staticmethod
            async def discover(timeout=1.0):
                return devices

        worker = transport_ble.BleTransportWorker()
        captured = []
        worker.scan_finished.connect(captured.append)

        with mock.patch.object(transport_ble, "BleakScanner", FakeScanner):
            asyncio.run(worker._scan(1.0))

        self.assertEqual(len(captured), 1)
        rows = captured[0]
        self.assertEqual([row["preferred"] for row in rows], [True, True, False])
        # Preferred rows sort first so the GUI pre-selects the OmniBCI device.
        self.assertEqual(rows[0]["key"], "A")


if __name__ == "__main__":
    unittest.main()
