from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from omnibci_app.protocol import (  # noqa: E402
    AdsFrameParser,
    crc16_ccitt,
    expand_frames_to_timeline,
    sequence_gap_size,
)


def make_frame(sequence: int) -> bytes:
    frame = bytearray(48)
    frame[:4] = bytes((0xA5, 0x5A, 1, 1))
    struct.pack_into("<I", frame, 4, sequence)
    frame[15] = 0x03
    frame[43] = 1
    frame[46:48] = struct.pack("<H", crc16_ccitt(frame[:46]))
    return bytes(frame)


class ProtocolTests(unittest.TestCase):
    def test_crc16_ccitt_reference_vector(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_sequence_gap_ignores_contiguous_frames(self):
        self.assertEqual(sequence_gap_size(10, 11), 0)
        self.assertEqual(sequence_gap_size(10, 13), 2)

    def test_parser_and_timeline_preserve_real_gap(self):
        parser = AdsFrameParser(lambda: np.float32(0.5))
        frames = parser.feed(make_frame(7) + make_frame(9))

        self.assertEqual([frame.sequence for frame in frames], [7, 9])
        timeline = expand_frames_to_timeline(frames, None, 0)
        values, valid, sequences = timeline[:3]

        self.assertEqual(values.shape, (8, 3))
        np.testing.assert_array_equal(valid, [True, False, True])
        np.testing.assert_array_equal(sequences, [7, 8, 9])
        self.assertTrue(np.isnan(values[:, 1]).all())


if __name__ == "__main__":
    unittest.main()
