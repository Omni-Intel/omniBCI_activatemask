"""Application startup and diagnostics lifecycle."""

from __future__ import annotations

import atexit

from .runtime import *  # noqa: F403
from .single_instance import SingleInstanceLock
from .window import MainWindow


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("全域智能 ADS1299 EEG 工作站")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))

    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        QtWidgets.QMessageBox.warning(
            None,
            f"OmniBCI V{APP_RELEASE_VERSION} 已在运行",
            f"已经有一个 OmniBCI V{APP_RELEASE_VERSION} 窗口正在运行。\n"
            "请使用现有窗口，或先退出该窗口后再启动。",
        )
        return 0
    atexit.register(instance_lock.release)

    from . import runtime

    runtime.APP_LOGGER, runtime.APP_LOG_PATH = configure_logging(LOG_DIR)
    APP_LOGGER, APP_LOG_PATH = runtime.APP_LOGGER, runtime.APP_LOG_PATH
    APP_LOGGER.info(
        "OmniBCI GUI release=V%d firmware=V19 protocol=V1 source=%s frozen=%s",
        APP_RELEASE_VERSION,
        __file__,
        IS_FROZEN,
    )

    original_excepthook = sys.excepthook

    def log_unhandled(exc_type, exc_value, exc_traceback):
        APP_LOGGER.critical(
            "unhandled main-thread exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        original_excepthook(exc_type, exc_value, exc_traceback)

    def log_thread_exception(args):
        APP_LOGGER.critical(
            "unhandled thread exception: %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def log_qt_message(message_type, context, message):
        level = {
            QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
            QtCore.QtMsgType.QtInfoMsg: logging.INFO,
            QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
            QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
            QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(message_type, logging.WARNING)
        location = f"{context.file}:{context.line}" if context and context.file else "Qt"
        APP_LOGGER.log(level, "%s %s", location, message)

    sys.excepthook = log_unhandled
    threading.excepthook = log_thread_exception
    QtCore.qInstallMessageHandler(log_qt_message)

    win = MainWindow()
    win.show()
    watchdog = HangWatchdog(
        5.0,
        lambda elapsed: APP_LOGGER.critical(
            "GUI unresponsive for %.1f seconds; stacks=%s",
            elapsed,
            dump_all_thread_stacks(LOG_DIR, elapsed),
        ),
    )
    watchdog_stop = watchdog.start()
    heartbeat_timer = QtCore.QTimer(app)
    heartbeat_timer.setInterval(500)
    heartbeat_timer.timeout.connect(watchdog.heartbeat)
    heartbeat_timer.start()
    APP_LOGGER.info("GUI event loop starting; log=%s", APP_LOG_PATH)
    try:
        return app.exec()
    finally:
        watchdog_stop.set()
        APP_LOGGER.info("GUI event loop stopped")
        shutdown_logging(APP_LOGGER)
        instance_lock.release()
