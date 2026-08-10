import json
import unittest

import numpy as np

from onmibci_sdk import (
    ExportResult,
    LocalClient,
    ProtocolError,
    _StreamIterator,
    connect_local,
)
from onmibci_stream import LocalStreamServer, MarkerEvent, StreamBatch


class LocalClientTests(unittest.TestCase):
    @staticmethod
    def make_batch(stream, generation=None, session_id="s1"):
        return StreamBatch.from_gui_matrix(
            stream=stream,
            values=np.arange(16, dtype=np.float32).reshape(8, 2),
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, False]),
            modes=np.array([0, 1], dtype=np.uint8),
            generation=generation,
            session_id=session_id,
        )

    def test_stream_raw_decodes_a_server_batch(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        iterator = None
        try:
            client = connect_local(port=server.port)
            iterator = client.stream_raw()
            expected = self.make_batch("raw", session_id=server.session_id)
            self.assertEqual(client.hello["stream"], "raw")
            server.publish(expected)

            item = next(iterator)

            self.assertEqual(item.stream, "raw")
            np.testing.assert_array_equal(item.values, expected.values)
            np.testing.assert_array_equal(item.sequence, expected.sequence)
        finally:
            if iterator is not None:
                iterator.close()
            server.stop()

    def test_send_marker_returns_acknowledged_event_and_streams_it(self):
        server = LocalStreamServer(port=0, session_id="api-1")
        server.start()
        server.begin_recording("rec-1", started_at=100.0)
        iterator = None
        try:
            client = connect_local(port=server.port)
            iterator = client.stream_raw()

            marker = client.send_marker("stimulus_on", 1, sequence=10)

            self.assertIsInstance(marker, MarkerEvent)
            self.assertEqual(marker.recording_id, "rec-1")
            self.assertEqual(next(iterator).code, "stimulus_on")
        finally:
            if iterator is not None:
                iterator.close()
            server.stop()

    def test_stop_and_export_return_typed_results(self):
        stop_calls = []

        def stop_handler():
            stop_calls.append(True)
            server.end_recording()
            return {"recording_id": "rec-1"}

        def export_handler(request, markers):
            return {
                "path": request["path"],
                "recording_id": "rec-1",
                "event_count": len(markers),
                "sample_count": 250,
            }

        server = LocalStreamServer(
            port=0,
            stop_handler=stop_handler,
            export_handler=export_handler,
        )
        server.start()
        server.begin_recording("rec-1")
        try:
            client = connect_local(port=server.port)
            self.assertEqual(client.stop_measurement()["recording_id"], "rec-1")
            result = client.export_bdf("out.bdf")

            self.assertIsInstance(result, ExportResult)
            self.assertEqual(result.path, "out.bdf")
            self.assertEqual(result.event_count, 0)
            self.assertEqual(result.sample_count, 250)
            self.assertEqual(stop_calls, [True])
        finally:
            server.stop()

    def test_stream_filtered_uses_the_filtered_subscription(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        iterator = None
        try:
            client = connect_local(port=server.port)
            iterator = client.stream_filtered()
            raw = self.make_batch("raw", session_id=server.session_id)
            filtered = self.make_batch(
                "filtered", generation=7, session_id=server.session_id
            )
            server.publish(raw)
            server.publish(filtered)

            item = next(iterator)

            self.assertEqual(item.stream, "filtered")
            self.assertEqual(item.generation, 7)
            np.testing.assert_array_equal(item.values, filtered.values)
        finally:
            if iterator is not None:
                iterator.close()
            server.stop()

    def test_stream_iterator_rejects_gap_for_another_stream(self):
        class FakeConnection:
            def __init__(self):
                self.closed = False

            def recv(self):
                return json.dumps(
                    {
                        "type": "gap",
                        "stream": "filtered",
                        "dropped_batches": 1,
                        "dropped_samples": 2,
                    }
                )

            def close(self):
                self.closed = True

        connection = FakeConnection()
        iterator = _StreamIterator(None, "raw", connection)

        with self.assertRaises(ProtocolError):
            next(iterator)
        self.assertTrue(connection.closed)

    def test_validate_hello_requires_stream_metadata(self):
        minimal_hello = {
            "type": "hello",
            "schema_version": 1,
            "stream": "raw",
            "session_id": "s1",
        }

        with self.assertRaises(ProtocolError):
            LocalClient._validate_hello(json.dumps(minimal_hello), "raw")


if __name__ == "__main__":
    unittest.main()
