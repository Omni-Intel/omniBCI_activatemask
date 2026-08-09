from contextlib import ExitStack
import json
import unittest

import numpy as np
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from onmibci_stream import LocalStreamServer, StreamBatch


class LocalStreamServerTests(unittest.TestCase):
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

    @staticmethod
    def subscribe(ws, stream):
        ws.send(json.dumps({"type": "subscribe", "stream": stream}))
        hello = json.loads(ws.recv())
        if hello.get("type") != "hello":
            raise AssertionError(f"unexpected hello: {hello}")
        return hello

    def test_subscriber_receives_hello_and_published_batch(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        try:
            with connect(f"ws://127.0.0.1:{server.port}/v1/stream") as ws:
                hello = self.subscribe(ws, "raw")
                self.assertEqual(hello["stream"], "raw")
                expected = self.make_batch("raw")
                server.publish(expected)
                decoded = StreamBatch.from_messages(ws.recv(), ws.recv())
                np.testing.assert_array_equal(decoded.values, expected.values)
                np.testing.assert_array_equal(decoded.sequence, expected.sequence)
        finally:
            server.stop()

    def test_two_subscribers_each_receive_the_same_stream_batch(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        try:
            with ExitStack() as stack:
                clients = [
                    stack.enter_context(
                        connect(f"ws://127.0.0.1:{server.port}/v1/stream")
                    ),
                    stack.enter_context(
                        connect(f"ws://127.0.0.1:{server.port}/v1/stream")
                    ),
                ]
                for ws in clients:
                    self.subscribe(ws, "raw")

                expected = self.make_batch("raw")
                server.publish(expected)
                decoded_batches = [
                    StreamBatch.from_messages(ws.recv(), ws.recv()) for ws in clients
                ]
                np.testing.assert_array_equal(
                    decoded_batches[0].values, decoded_batches[1].values
                )
                np.testing.assert_array_equal(
                    decoded_batches[0].sequence, decoded_batches[1].sequence
                )
        finally:
            server.stop()

    def test_non_object_subscription_is_rejected(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        try:
            with connect(f"ws://127.0.0.1:{server.port}/v1/stream") as ws:
                ws.send("[]")
                with self.assertRaises(ConnectionClosed) as raised:
                    ws.recv()
                self.assertEqual(raised.exception.rcvd.code, 1008)
        finally:
            server.stop()

    def test_publish_rejects_a_batch_from_another_session(self):
        server = LocalStreamServer(session_id="s1")
        foreign_batch = self.make_batch("raw", session_id="other")
        with self.assertRaises(ValueError):
            server.publish(foreign_batch)


if __name__ == "__main__":
    unittest.main()
