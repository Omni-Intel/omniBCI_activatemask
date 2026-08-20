"""Small Qt widgets and worker helpers used by the main window."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .constants import APP_LOGGER

class SoftTriggerWindow(QtWidgets.QWidget):
    def __init__(self, send_trigger, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self._send_trigger = send_trigger
        self.setWindowTitle("软 Trigger")
        self.setFixedWidth(300)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.trigger_spin = QtWidgets.QSpinBox()
        self.trigger_spin.setRange(1, 255)
        self.trigger_spin.setValue(1)
        self.trigger_spin.setKeyboardTracking(False)
        form.addRow("Trigger 编号", self.trigger_spin)
        layout.addLayout(form)

        self.send_button = QtWidgets.QPushButton("发送")
        self.send_button.clicked.connect(self.send_current_trigger)
        layout.addWidget(self.send_button)

        self.result_label = QtWidgets.QLabel("等待发送")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self)
        self.space_shortcut.setContext(QtCore.Qt.WindowShortcut)
        self.space_shortcut.setAutoRepeat(False)
        self.space_shortcut.activated.connect(self.send_current_trigger)

    def send_current_trigger(self):
        number = int(self.trigger_spin.value())
        try:
            marker = self._send_trigger(number)
            sequence = "---" if marker.sequence is None else str(marker.sequence)
            self.result_label.setText(f"已发送 Trigger {number} · Seq {sequence}")
        except Exception as exc:
            self.result_label.setText(str(exc))
            APP_LOGGER.warning("GUI soft trigger failed: %s", exc)


class ClockAxisItem(pg.AxisItem):
    """EEG paper time axis formatted as HH:MM:SS."""

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            seconds = max(0, int(round(value)))
            hours, rem = divmod(seconds, 3600)
            minutes, secs = divmod(rem, 60)
            labels.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
        return labels


class PsdWorkerSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class PsdWorker(QtCore.QRunnable):
    """Run the relatively expensive PSD/quality calculation off the GUI thread."""

    def __init__(
        self, owner, request_id: int, x: np.ndarray, valid: np.ndarray,
        seq: np.ndarray, mode: np.ndarray, sos_band: np.ndarray,
        use_notch: bool, live_fast: bool = False,
    ):
        super().__init__()
        self.owner = owner
        self.request_id = request_id
        self.x = np.asarray(x, dtype=float)
        self.valid = np.asarray(valid, dtype=bool)
        self.seq = np.asarray(seq, dtype=np.uint32)
        self.mode = np.asarray(mode, dtype=np.uint8)
        self.sos_band = np.asarray(sos_band, dtype=float).copy()
        self.use_notch = bool(use_notch)
        self.live_fast = bool(live_fast)
        self.signals = PsdWorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            if self.live_fast:
                result = self.owner.compute_live_psd_fast(
                    self.x, self.valid, self.seq, self.mode,
                    sos_band=self.sos_band, use_notch=self.use_notch,
                )
            else:
                result = self.owner.compute_alpha_from_window(
                    self.x, self.valid, self.seq, self.mode,
                    sos_band=self.sos_band, use_notch=self.use_notch,
                )
            self.signals.finished.emit(self.request_id, (result, self.x, self.valid))
        except Exception as exc:  # pragma: no cover - surfaced in the GUI
            self.signals.failed.emit(self.request_id, str(exc))


