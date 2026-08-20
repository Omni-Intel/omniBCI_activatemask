import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np

from onmibci_stream import LocalStreamServer, StreamBatch


SDK_PATH = Path(__file__).parents[1] / "public_sdk" / "omnibci_sdk.py"
SPEC = importlib.util.spec_from_file_location("public_omnibci_sdk", SDK_PATH)
sdk = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sdk
SPEC.loader.exec_module(sdk)


class PublicSdkTests(unittest.TestCase):
    def test_stream_and_trigger_work_against_local_api(self):
        server = LocalStreamServer(port=0, session_id="public-api-test")
        server.start()
        server.begin_recording("recording-1")
        stream = None
        try:
            client = sdk.connect_local(port=server.port)
            stream = client.stream_raw()
            expected = StreamBatch.from_gui_matrix(
                stream="raw",
                values=np.arange(16, dtype=np.float32).reshape(8, 2),
                sequence=np.array([10, 11], dtype=np.uint32),
                valid=np.array([True, True]),
                modes=np.array([0, 0], dtype=np.uint8),
                generation=None,
                session_id=server.session_id,
            )
            server.publish(expected)

            batch = next(stream)
            event = client.send_trigger(23, sequence=11)

            np.testing.assert_array_equal(batch.values, expected.values)
            self.assertEqual(event.code, "soft_trigger")
            self.assertEqual(event.value, 23)
            self.assertEqual(event.sequence, 11)
        finally:
            if stream is not None:
                stream.close()
            server.stop()

    def test_trigger_range_is_validated_locally(self):
        client = sdk.connect_local()
        for number in (0, 256, True):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    client.send_trigger(number)


if __name__ == "__main__":
    unittest.main()
