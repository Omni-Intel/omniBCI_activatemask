import struct
import unittest

from onmibci_gui.frames import AdsFrameParser, crc16_ccitt


class RealtimeTriggerTests(unittest.TestCase):
    def test_event_packet_is_decoded_separately_from_ads_frame(self):
        packet = bytearray(48)
        packet[:4] = bytes((0xA5, 0x5A, 1, 2))
        struct.pack_into("<I", packet, 4, 23)
        struct.pack_into("<Q", packet, 8, 123456789)
        packet[16] = 6
        packet[17] = 1
        struct.pack_into("<I", packet, 18, 1234)
        struct.pack_into("<I", packet, 22, 5678)
        struct.pack_into("<H", packet, 46, crc16_ccitt(packet[:46]))

        parser = AdsFrameParser(lambda: 1.0)
        self.assertEqual(parser.feed(bytes(packet)), [])
        event = parser.drain_events()[0]

        self.assertEqual(event.sequence, 23)
        self.assertEqual(event.start_time_us, 123456789)
        self.assertEqual(event.event_id, 6)
        self.assertEqual(event.anchor_frame_sequence, 1234)
        self.assertEqual(event.anchor_frame_timestamp_us, 5678)

    def test_ads_frame_does_not_carry_event_marker(self):
        frame = bytearray(48)
        frame[:4] = bytes((0xA5, 0x5A, 1, 1))
        struct.pack_into("<I", frame, 4, 1234)
        struct.pack_into("<I", frame, 8, 5678)
        frame[12:15] = b"\xc0\x00\x00"
        frame[15] = 0x03
        frame[43] = 1
        struct.pack_into("<H", frame, 46, crc16_ccitt(frame[:46]))

        decoded = AdsFrameParser(lambda: 1.0).feed(bytes(frame))[0]

        self.assertEqual(decoded.mode, 1)

    def test_data_packet_drain_excludes_parallel_event_packets(self):
        data = bytearray(48)
        data[:4] = bytes((0xA5, 0x5A, 1, 1))
        struct.pack_into("<I", data, 4, 9)
        data[12:15] = b"\xc0\x00\x00"
        data[15] = 0x03
        struct.pack_into("<H", data, 46, crc16_ccitt(data[:46]))

        event = bytearray(48)
        event[:4] = bytes((0xA5, 0x5A, 1, 2))
        event[16] = 0
        struct.pack_into("<H", event, 46, crc16_ccitt(event[:46]))

        parser = AdsFrameParser(lambda: 1.0)
        parser.feed(bytes(event) + bytes(data))
        self.assertEqual(parser.drain_data_packets(), [bytes(data)])
