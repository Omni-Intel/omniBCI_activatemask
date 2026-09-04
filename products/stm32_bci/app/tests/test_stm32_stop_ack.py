"""Exercise the production stop handshake without requiring Qt or hardware."""
import ast
from pathlib import Path
import time
import unittest

SOURCE = Path(__file__).parents[1] / "onmibci_gui" / "transport_control.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
method = next(n for c in tree.body if isinstance(c, ast.ClassDef)
              for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "stop_stm32_recording")
namespace = {"time": time}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), namespace)


class StopAckTest(unittest.TestCase):
    def make_device(self, replies):
        class Device:
            stop = namespace["stop_stm32_recording"]
            def __init__(self):
                self.sent = []
            def transport_write(self, data):
                self.sent.append(data)
            def read_config_ack(self, command, timeout, expected_argument):
                self.command = command
                self.expected_argument = expected_argument
                return replies.pop(0) if replies else None
        return Device()

    def test_waits_for_closed_ack(self):
        closed = {"verified": True, "channel_register": 0}
        d = self.make_device([{"verified": False, "channel_register": 2}, closed])
        self.assertEqual(d.stop(), closed)
        self.assertEqual(d.sent, [b"s", b"\xab\x01", b"\xab\x02"])
        self.assertEqual(d.expected_argument, 2)

    def test_missing_ack_does_not_report_success(self):
        d = self.make_device([])
        with self.assertRaises(RuntimeError):
            d.stop(timeout=0.001)

    def test_running_ack_is_not_stop_ack(self):
        d = self.make_device([{"verified": True, "channel_register": 1}])
        with self.assertRaises(RuntimeError):
            d.stop(timeout=0.001)

    def test_sd_close_error_is_not_silent_success(self):
        d = self.make_device([{"verified": True, "channel_register": 0, "bias_n": 1}])
        with self.assertRaises(RuntimeError):
            d.stop()

if __name__ == "__main__":
    unittest.main()
