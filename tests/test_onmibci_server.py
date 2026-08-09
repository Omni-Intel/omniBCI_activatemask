from contextlib import ExitStack
import json
import threading
import unittest

import numpy as np
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from onmibci_stream import LocalStreamServer, StreamBatch, _Subscriber, _WirePacket


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

    def test_wrong_path_is_rejected(self):
        server = LocalStreamServer(port=0, session_id="s1")
        server.start()
        try:
            with self.assertRaises(InvalidStatus):
                with connect(f"ws://127.0.0.1:{server.port}/wrong-path"):
                    pass
        finally:
            server.stop()

    def test_publish_rejects_a_batch_from_another_session(self):
        server = LocalStreamServer(session_id="s1")
        foreign_batch = self.make_batch("raw", session_id="other")
        with self.assertRaises(ValueError):
            server.publish(foreign_batch)

    def test_bounded_subscriber_queue_reports_dropped_batches(self):
        subscriber = _Subscriber("raw", queue_size=1)
        self.assertTrue(
            subscriber.enqueue(_WirePacket("first", b"", samples=2))
        )
        self.assertFalse(
            subscriber.enqueue(_WirePacket("second", b"", samples=3))
        )

        gap = subscriber.take_gap()

        self.assertEqual(gap.stream, "raw")
        self.assertEqual(gap.dropped_batches, 1)
        self.assertEqual(gap.dropped_samples, 2)
        self.assertEqual(subscriber.queue.get_nowait().header, "second")
        self.assertIsNone(subscriber.take_gap())

    def test_stalled_websocket_sender_receives_an_explicit_gap(self):
        server = LocalStreamServer(port=0, queue_size=1, session_id="s1")
        server.start()
        release = threading.Event()
        entered = threading.Event()
        try:
            with connect(f"ws://127.0.0.1:{server.port}/v1/stream") as ws:
                self.subscribe(ws, "raw")

                def stall_loop():
                    entered.set()
                    release.wait(timeout=2.0)

                server._loop.call_soon_threadsafe(stall_loop)
                self.assertTrue(entered.wait(timeout=2.0))
                server.publish(self.make_batch("raw", session_id="s1"))
                server.publish(self.make_batch("raw", session_id="s1"))
                release.set()

                gap = json.loads(ws.recv())
                self.assertEqual(gap["type"], "gap")
                self.assertEqual(gap["dropped_batches"], 1)
                decoded = StreamBatch.from_messages(ws.recv(), ws.recv())
                self.assertEqual(decoded.stream, "raw")
        finally:
            release.set()
            server.stop()


if __name__ == "__main__":
    unittest.main()
