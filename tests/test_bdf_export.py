import sys
import tempfile
import types
import unittest
from pathlib import Path
import importlib.util

import numpy as np

from ads1299_eeg_gui_native import (
    CHANNELS,
    FRAME_BYTES,
    MainWindow,
    crc16_ccitt,
)
from onmibci_stream import MarkerEvent


def make_frame(sequence: int, count: int) -> bytes:
    frame = bytearray(FRAME_BYTES)
    frame[0:4] = bytes((0xA5, 0x5A, 1, 1))
    frame[4:8] = int(sequence).to_bytes(4, "little")
    frame[15] = 0x03
    for channel in range(CHANNELS):
        value = int(count + channel)
        offset = 16 + channel * 3
        frame[offset:offset + 3] = value.to_bytes(3, "big", signed=True)
    frame[43] = 0
    frame[46:48] = int(crc16_ccitt(frame[:46])).to_bytes(2, "little")
    return bytes(frame)


class _FakeEdfWriter:
    instances = []

    def __init__(self, path, channel_count, file_type):
        self.path = path
        self.channel_count = channel_count
        self.file_type = file_type
        self.annotations = []
        self.samples = None
        self.__class__.instances.append(self)

    def setSignalHeaders(self, headers):
        self.headers = headers

    def setPatientCode(self, value):
        pass

    def setEquipment(self, value):
        pass

    def writeSamples(self, samples):
        self.samples = samples

    def writeAnnotation(self, onset, duration, description):
        self.annotations.append((onset, duration, description))

    def close(self):
        pass


class BdfExportTests(unittest.TestCase):
    def test_save_bdf_uses_session_channel_names(self):
        fake_pyedflib = types.SimpleNamespace(
            EdfWriter=_FakeEdfWriter,
            FILETYPE_BDFPLUS=42,
        )
        previous = sys.modules.get("pyedflib")
        sys.modules["pyedflib"] = fake_pyedflib
        _FakeEdfWriter.instances.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                window = MainWindow.__new__(MainWindow)
                window.offline_uv = np.zeros((CHANNELS, 2), dtype=np.float32)
                window.offline_valid = np.ones(2, dtype=bool)
                window.channel_names = [f"INP{index}" for index in range(1, 9)]

                window.save_bdf(Path(directory) / "named.bdf")

                labels = [
                    header["label"]
                    for header in _FakeEdfWriter.instances[0].headers
                ]
                self.assertEqual(labels, window.channel_names)
        finally:
            if previous is None:
                sys.modules.pop("pyedflib", None)
            else:
                sys.modules["pyedflib"] = previous

    def test_channel_name_validation_rejects_invalid_or_duplicate_names(self):
        window = MainWindow.__new__(MainWindow)
        window.channel_names = [f"CH{index}" for index in range(1, 9)]

        self.assertEqual(window.validated_channel_name(0, " Fp1 "), "Fp1")
        for invalid in ("", "CH2", "\u989d\u6781", "12345678901234567"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    window.validated_channel_name(0, invalid)

    def test_export_recording_bdf_reads_all_segments_and_writes_markers(self):
        fake_pyedflib = types.SimpleNamespace(
            EdfWriter=_FakeEdfWriter,
            FILETYPE_BDFPLUS=42,
        )
        previous = sys.modules.get("pyedflib")
        sys.modules["pyedflib"] = fake_pyedflib
        _FakeEdfWriter.instances.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                segment_one = Path(directory) / "minute01.bin"
                segment_two = Path(directory) / "minute02.bin"
                segment_one.write_bytes(make_frame(100, 10))
                segment_two.write_bytes(make_frame(101, 20))
                output = Path(directory) / "recording.bdf"

                window = MainWindow.__new__(MainWindow)
                window.channel_gains = np.full(CHANNELS, 24, dtype=np.int16)
                window.channel_names = [f"EEG{index}" for index in range(1, 9)]
                window.raw_writer = types.SimpleNamespace(
                    snapshot=lambda: {
                        "segments": [
                            {"path": str(segment_one)},
                            {"path": str(segment_two)},
                        ]
                    }
                )
                result = window.export_recording_bdf(
                    output,
                    (
                        MarkerEvent(
                            "e1", "api", "rec", "stimulus_on", 1,
                            100.0, 100, 0.0, "start",
                        ),
                        MarkerEvent(
                            "e2", "api", "rec", "stimulus_off", 0,
                            100.1, 101, 0.0, "stop",
                        ),
                    ),
                    recording_id="rec",
                    recording_started_at=100.0,
                    first_sequence=100,
                )

                self.assertEqual(result["sample_count"], 2)
                self.assertEqual(result["event_count"], 2)
                self.assertEqual(len(_FakeEdfWriter.instances[0].samples[0]), 2)
                self.assertEqual(
                    [header["label"] for header in _FakeEdfWriter.instances[0].headers],
                    window.channel_names,
                )
                self.assertEqual(len(_FakeEdfWriter.instances[0].annotations), 2)
                self.assertTrue(
                    any(
                        "stimulus_on" in annotation[2]
                        for annotation in _FakeEdfWriter.instances[0].annotations
                    )
                )
        finally:
            if previous is None:
                sys.modules.pop("pyedflib", None)
            else:
                sys.modules["pyedflib"] = previous

    @unittest.skipUnless(
        importlib.util.find_spec("pyedflib"),
        "optional BDF export dependency is not installed",
    )
    def test_real_bdfplus_contains_marker_annotations(self):
        import pyedflib

        with tempfile.TemporaryDirectory() as directory:
            segment = Path(directory) / "minute01.bin"
            output = Path(directory) / "recording.bdf"
            segment.write_bytes(
                b"".join(make_frame(100 + index, index) for index in range(500))
            )
            window = MainWindow.__new__(MainWindow)
            window.channel_gains = np.full(CHANNELS, 24, dtype=np.int16)
            window.channel_names = [f"CH{index}" for index in range(1, 9)]
            window.raw_writer = types.SimpleNamespace(
                snapshot=lambda: {"segments": [{"path": str(segment)}]}
            )
            window.export_recording_bdf(
                output,
                (
                    MarkerEvent(
                        "e1", "api", "rec", "stimulus_on", 1,
                        100.0, 100, 0.0, "start",
                    ),
                    MarkerEvent(
                        "e2", "api", "rec", "stimulus_off", 0,
                        100.1, None, 0.0, "stop",
                    ),
                ),
                recording_id="rec",
                recording_started_at=100.0,
                first_sequence=100,
            )
            reader = pyedflib.EdfReader(str(output))
            try:
                onsets, _durations, descriptions = reader.readAnnotations()
            finally:
                reader.close()
            self.assertTrue(any("stimulus_on" in text for text in descriptions))
            self.assertTrue(any("stimulus_off" in text for text in descriptions))
            off_index = next(
                index
                for index, text in enumerate(descriptions)
                if "stimulus_off" in text
            )
            self.assertAlmostEqual(float(onsets[off_index]), 0.1, places=3)


if __name__ == "__main__":
    unittest.main()
