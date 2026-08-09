import json
import unittest

import numpy as np

from onmibci_stream import StreamBatch, publish_gui_matrix


class _CaptureServer:
    def __init__(self):
        self.batch = None

    def publish(self, batch):
        self.batch = batch


class StreamBatchTests(unittest.TestCase):
    def test_raw_boundary_preserves_rail_values(self):
        raw_values = np.zeros((8, 2), dtype=np.float32)
        raw_values[0, 0] = 8388607.0
        capture = _CaptureServer()

        batch = publish_gui_matrix(
            capture,
            stream="raw",
            values=raw_values,
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, True]),
            modes=np.array([0, 0], dtype=np.uint8),
            generation=None,
            session_id="s1",
        )

        self.assertEqual(float(batch.values[0, 0]), 8388607.0)
        self.assertIs(capture.batch, batch)

    def test_gui_matrix_round_trip_preserves_values_and_metadata(self):
        gui_values = np.arange(16, dtype=np.float32).reshape(8, 2)
        batch = StreamBatch.from_gui_matrix(
            stream="raw",
            values=gui_values,
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, False]),
            modes=np.array([0, 1], dtype=np.uint8),
            generation=None,
            session_id="s1",
        )

        header, payload = batch.to_messages()
        decoded = StreamBatch.from_messages(header, payload)

        np.testing.assert_array_equal(decoded.values, gui_values.T)
        np.testing.assert_array_equal(decoded.sequence, [10, 11])
        np.testing.assert_array_equal(decoded.valid, [True, False])
        np.testing.assert_array_equal(decoded.modes, [0, 1])
        self.assertEqual(decoded.stream, "raw")
        self.assertIsNone(decoded.generation)

    def test_filtered_batch_preserves_generation(self):
        batch = StreamBatch.from_gui_matrix(
            stream="filtered",
            values=np.ones((8, 1), dtype=np.float32),
            sequence=np.array([100], dtype=np.uint32),
            valid=np.array([True]),
            modes=np.array([0], dtype=np.uint8),
            generation=7,
            session_id="s1",
        )

        decoded = StreamBatch.from_messages(*batch.to_messages())

        self.assertEqual(decoded.stream, "filtered")
        self.assertEqual(decoded.generation, 7)

    def test_mismatched_metadata_length_is_rejected(self):
        with self.assertRaises(ValueError):
            StreamBatch(
                stream="raw",
                values=np.zeros((2, 8), dtype=np.float32),
                sequence=np.array([1], dtype=np.uint32),
                valid=np.array([True, True]),
                modes=np.array([0, 0]),
                generation=None,
                session_id="s1",
            )

    def test_wire_metadata_rejects_type_coercion(self):
        batch = StreamBatch.from_gui_matrix(
            stream="raw",
            values=np.zeros((8, 1), dtype=np.float32),
            sequence=np.array([1], dtype=np.uint32),
            valid=np.array([True]),
            modes=np.array([0], dtype=np.uint8),
            generation=None,
            session_id="s1",
        )
        header, payload = batch.to_messages()
        metadata = json.loads(header)

        invalid_fields = {
            "valid-string": {"valid": ["false"]},
            "shape-bool": {"shape": [True, 8]},
            "sequence-negative": {"sequence": [-1]},
            "modes-fraction": {"modes": [1.5]},
            "session-number": {"session_id": 123},
            "channels-string": {"channels": "ABCDEFGH"},
            "sample-rate-string": {"sample_rate": "250"},
        }
        for label, update in invalid_fields.items():
            with self.subTest(label):
                invalid = dict(metadata)
                invalid.update(update)
                with self.assertRaises((TypeError, ValueError)):
                    StreamBatch.from_messages(json.dumps(invalid), payload)


if __name__ == "__main__":
    unittest.main()
