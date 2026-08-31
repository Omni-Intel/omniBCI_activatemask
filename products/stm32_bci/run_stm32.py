"""Product-only startup adaptations for the unchanged STM32 reference snapshot."""

from functools import partial
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parent / "app"
LOCK_PATH = Path(tempfile.gettempdir()) / "omnibci_stm32_bci.lock"


def prepare_app():
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(APP_DIR))
    os.chdir(APP_DIR)

    from PySide6 import QtCore
    from onmibci_gui import app, window
    from onmibci_gui.single_instance import SingleInstanceLock
    from onmibci_gui.transport_control import TransportControlMixin
    from onmibci_stream import LocalStreamServer

    settings = QtCore.QSettings(str(APP_DIR / "stm32_bci.ini"), QtCore.QSettings.IniFormat)
    settings.setFallbacksEnabled(False)
    settings.setValue("mcu_family", "stm32")
    settings.sync()
    if settings.status() != QtCore.QSettings.NoError:
        raise RuntimeError(f"Cannot read/write STM32 settings: {settings.fileName()}")

    # ponytail: in-memory adaptation is tied to c2ecbd6; replace with native
    # product configuration when the later STM32 GUI is developed.
    window.QtCore = SimpleNamespace(**{
        **vars(QtCore),
        "QSettings": lambda organization, application: settings,
    })
    window.MCU_ITEMS = (("STM32H563 + E73", "stm32"),)
    window.BLE_AVAILABLE = False
    TransportControlMixin.selected_mcu = lambda self: "stm32"
    TransportControlMixin.selected_transport = lambda self: "serial"
    window.LocalStreamServer = partial(LocalStreamServer, port=8767)
    app.SingleInstanceLock = partial(SingleInstanceLock, path=LOCK_PATH)
    return app


if __name__ == "__main__":
    raise SystemExit(prepare_app().main())
