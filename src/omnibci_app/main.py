
"""Application entry point."""

from __future__ import annotations

import sys

from PySide6 import QtGui, QtWidgets

from .constants import (
    APP_ICON_PATH,
)
from .ui import MainWindow


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("全域智能 ADS1299 EEG 工作站")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
