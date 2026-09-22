"""Discord Member Scout - entry point.

Qt and asyncio share a single event loop via qasync: discord.py's gateway
connection, REST calls and the scanner run as asyncio tasks, while Qt keeps
painting and handling input. Heavy database writes are offloaded to worker
threads with ``asyncio.to_thread``; every UI update happens on the GUI thread
through Qt signals.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys


def _check_python() -> None:
    if sys.version_info < (3, 11):
        sys.stderr.write("Discord Member Scout requires Python 3.11 or newer.\n")
        sys.exit(1)


_check_python()

import qasync  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon  # noqa: E402

from app import APP_ID, APP_NAME, __version__  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.database import Database, DatabaseError  # noqa: E402
from app.utils import PROJECT_ROOT, data_dir, setup_logging  # noqa: E402
from ui.components.icons import app_icon  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import ThemeManager  # noqa: E402

log = logging.getLogger("scout")


def _build_tray(app: QApplication) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.info("System tray not available; notifications will use plyer")
        return None
    tray = QSystemTrayIcon(app_icon(), app)
    tray.setToolTip(APP_NAME)
    tray.show()
    return tray


def _attach_tray_menu(tray: QSystemTrayIcon, window: MainWindow, controller: AppController) -> None:
    menu = QMenu()
    menu.addAction("Open Discord Member Scout", window.bring_to_front)
    menu.addAction("Scan Now", controller.scan_now)
    pause_action = menu.addAction("Pause Monitoring", controller.toggle_pause)
    controller.paused_changed.connect(
        lambda paused: pause_action.setText("Resume Monitoring" if paused else "Pause Monitoring")
    )
    menu.addSeparator()
    menu.addAction("Quit", window.close)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: window.bring_to_front()
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick)
        else None
    )
    window._tray_menu = menu  # keep a reference alive


async def run_app(app: QApplication) -> int:
    loop = asyncio.get_running_loop()
    close_event = asyncio.Event()

    try:
        db = Database(data_dir() / "scout.db")
    except DatabaseError as exc:
        QMessageBox.critical(None, APP_NAME, f"Database error:\n\n{exc}")
        return 1

    tray = _build_tray(app)
    controller = AppController(db, tray, app_icon())
    ThemeManager.instance().apply(controller.settings.theme)

    window = MainWindow(controller)
    window.close_requested.connect(close_event.set)
    if tray is not None:
        _attach_tray_menu(tray, window, controller)

    # Ctrl+C in the terminal closes the window gracefully.
    try:
        loop.add_signal_handler(signal.SIGINT, window.close)
    except (NotImplementedError, RuntimeError, AttributeError):
        signal.signal(signal.SIGINT, lambda *_: QTimer.singleShot(0, window.close))

    window.show()
    controller.start()

    await close_event.wait()

    window.hide()
    try:
        await asyncio.wait_for(controller.shutdown(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Shutdown timed out; exiting anyway")
    if tray is not None:
        tray.hide()
    return 0


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    log_path = setup_logging()
    log.info("%s %s starting (log: %s)", APP_NAME, __version__, log_path)

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_ID)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)  # shutdown is driven by run_app()

    # Keep Python signal handlers responsive while Qt owns the main loop.
    heartbeat = QTimer()
    heartbeat.start(250)
    heartbeat.timeout.connect(lambda: None)

    try:
        return qasync.run(run_app(app))
    except (KeyboardInterrupt, SystemExit):
        return 0


if __name__ == "__main__":
    sys.exit(main())
