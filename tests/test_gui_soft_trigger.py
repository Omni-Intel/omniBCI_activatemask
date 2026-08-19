import os
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from ads1299_eeg_gui_native import MainWindow, SoftTriggerWindow


class SoftTriggerWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_range_and_send_button(self):
        sent = []
        window = SoftTriggerWindow(
            lambda number: sent.append(number) or types.SimpleNamespace(sequence=7)
        )

        self.assertEqual((window.trigger_spin.minimum(), window.trigger_spin.maximum()), (1, 255))
        self.assertFalse(window.space_shortcut.autoRepeat())
        window.trigger_spin.setValue(42)
        window.send_button.click()

        self.assertEqual(sent, [42])

    def test_close_then_show_again(self):
        window = SoftTriggerWindow(lambda number: types.SimpleNamespace(sequence=None))
        window.show()
        self.app.processEvents()
        window.close()
        self.app.processEvents()
        self.assertFalse(window.isVisible())

        window.show()
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        window.close()


class GuiSoftTriggerTests(unittest.TestCase):
    def test_send_soft_trigger_uses_latest_sequence(self):
        calls = []
        window = MainWindow.__new__(MainWindow)
        window.streaming = True
        window.last_seq = 456
        window.set_status = lambda text: None
        window.stream_server = types.SimpleNamespace(
            add_marker=lambda *args, **kwargs: calls.append((args, kwargs))
            or types.SimpleNamespace(sequence=kwargs["sequence"])
        )

        marker = window.send_soft_trigger(23)

        self.assertEqual(calls[0][0], ("soft_trigger", 23))
        self.assertEqual(calls[0][1]["sequence"], 456)
        self.assertEqual(marker.sequence, 456)

    def test_send_soft_trigger_rejects_outside_acquisition(self):
        window = MainWindow.__new__(MainWindow)
        window.streaming = False
        window.last_seq = None
        window.stream_server = None

        with self.assertRaisesRegex(RuntimeError, "开始采集"):
            window.send_soft_trigger(1)

    def test_send_soft_trigger_rejects_out_of_range_number(self):
        window = MainWindow.__new__(MainWindow)
        window.streaming = True
        window.last_seq = 1
        window.set_status = lambda text: None
        window.stream_server = types.SimpleNamespace(add_marker=lambda *args, **kwargs: None)

        for number in (0, 256):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    window.send_soft_trigger(number)


if __name__ == "__main__":
    unittest.main()

