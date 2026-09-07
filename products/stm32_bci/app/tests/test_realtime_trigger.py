import struct
import unittest

from onmibci_gui.frames import AdsFrameParser, crc16_ccitt


class RealtimeTriggerTests(unittest.TestCase):
    def test_trigger_marker_is_decoded_without_changing_mode(self):
        frame = bytearray(48)
        frame[:4] = bytes((0xA5, 0x5A, 1, 1))
        struct.pack_into("<I", frame, 4, 1234)
        struct.pack_into("<I", frame, 8, 5678)
        frame[12:15] = b"\xc0\x00\x00"
        frame[15] = 0x03
        frame[43] = 0x80 | 1  # STM32 marker + SRB1 mode.
        struct.pack_into("<H", frame, 46, crc16_ccitt(frame[:46]))

        decoded = AdsFrameParser(lambda: 1.0).feed(bytes(frame))[0]

        self.assertTrue(decoded.triggered)
        self.assertEqual(decoded.mode, 1)
        self.assertEqual(decoded.sequence, 1234)
        self.assertEqual(decoded.timestamp_us, 5678)

    def test_normal_frame_has_no_trigger_marker(self):
        frame = bytearray(48)
        frame[:4] = bytes((0xA5, 0x5A, 1, 1))
        frame[12:15] = b"\xc0\x00\x00"
        frame[15] = 0x03
        frame[43] = 1
        struct.pack_into("<H", frame, 46, crc16_ccitt(frame[:46]))

        decoded = AdsFrameParser(lambda: 1.0).feed(bytes(frame))[0]

        self.assertFalse(decoded.triggered)
        self.assertEqual(decoded.mode, 1)
