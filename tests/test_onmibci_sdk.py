import unittest

import numpy as np

from onmibci_sdk import connect_local
from onmibci_stream import LocalStreamServer, StreamBatch


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


if __name__ == "__main__":
    unittest.main()
