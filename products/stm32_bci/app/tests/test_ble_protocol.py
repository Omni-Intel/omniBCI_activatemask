import unittest

from onmibci_ble_protocol import (
    MSG_GET_CONFIG,
    MSG_RESPONSE,
    ProtocolError,
    decode_packet,
    encode_packet,
    decode_config_snapshot,
    encode_set_config,
)


class BleProtocolTests(unittest.TestCase):
    def test_round_trip_preserves_transaction_and_payload(self):
        wire = encode_packet(MSG_GET_CONFIG, 0x1234, b"\x01\x02")

        packet = decode_packet(wire)

        self.assertEqual(packet.message_type, MSG_GET_CONFIG)
        self.assertEqual(packet.request_id, 0x1234)
        self.assertEqual(packet.payload, b"\x01\x02")

    def test_response_uses_same_transaction_id(self):
        wire = encode_packet(MSG_RESPONSE | MSG_GET_CONFIG, 77, b"ok")

        packet = decode_packet(wire)

        self.assertEqual(packet.request_id, 77)
        self.assertEqual(packet.message_type, MSG_RESPONSE | MSG_GET_CONFIG)

    def test_crc_error_is_rejected(self):
        wire = bytearray(encode_packet(MSG_GET_CONFIG, 1, b"config"))
        wire[-1] ^= 0x01

        with self.assertRaisesRegex(ProtocolError, "CRC"):
            decode_packet(wire)

    def test_truncated_packet_is_rejected(self):
        wire = encode_packet(MSG_GET_CONFIG, 1, b"config")

        with self.assertRaisesRegex(ProtocolError, "length"):
            decode_packet(wire[:-1])

    def test_set_config_is_srb1_only(self):
        payload = encode_set_config(1, 0x1F, 0x1F, 0x03, [24] * 8)

        self.assertEqual(payload, bytes((1, 0x1F, 0x1F, 0x03, 24, 24, 24, 24, 24, 24, 24, 24)))

    def test_full_register_snapshot_decodes(self):
        payload = (
            bytes((0,))
            + (9).to_bytes(4, "little")
            + bytes(
                (
                    1,
                    1,
                    0x1F,
                    0x1F,
                    0x03,
                    0x96,
                    0xC0,
                    0xEC,
                    0x60,
                    0x60,
                    0x60,
                    0x60,
                    0x60,
                    0xE0,
                    0xE0,
                    0xE0,
                    0x1F,
                    0x00,
                    0x03,
                    0x00,
                    0x20,
                )
            )
        )

        snapshot = decode_config_snapshot(payload)

        self.assertEqual(snapshot.generation, 9)
        self.assertTrue(snapshot.verified)
        self.assertEqual(snapshot.channel_registers, (0x60,) * 5 + (0xE0,) * 3)
        self.assertEqual(snapshot.misc1, 0x20)

    def test_extended_snapshot_decodes_runtime_sample_rate(self):
        payload = bytearray(29)
        payload[0] = 0
        payload[5] = 1
        payload[6] = 1
        payload[7] = 0xFF
        payload[10] = 0x94
        payload[13:21] = bytes((0x60,)) * 8
        payload[25] = 0x20
        payload[26] = 2
        payload[27:29] = (1000).to_bytes(2, "little")

        snapshot = decode_config_snapshot(payload)

        self.assertEqual(snapshot.config1, 0x94)
        self.assertEqual(snapshot.sample_rate_code, 2)
        self.assertEqual(snapshot.sample_rate_hz, 1000)


if __name__ == "__main__":
    unittest.main()
