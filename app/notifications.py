"""Native desktop notifications.

Primary backend: :class:`QSystemTrayIcon` messages. They are rendered by the
OS notification centre (Windows toast, macOS Notification Center, freedesktop
notifications on Linux) and, unlike plyer, report clicks - which we use to
bring the window to the foreground.

Fallback backend: :mod:`plyer` (used when no system tray is available).

Notifications are paced (one every ``PACE_MS``) because OS notification
centres silently drop bursts. Delivery state is persisted by the caller in
SQLite (``members.notification_sent``) so restarts never re-notify.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from app import APP_NAME
from app.models import MemberRecord
from app.utils import data_dir, format_long_date, plural

log = logging.getLogger(__name__)

PACE_MS = 1500
MESSAGE_DURATION_MS = 6000


@dataclass(slots=True)
class _Pending:
    title: str
    body: str


def _ensure_chime(path: Path) -> Path:
    """Create a short, soft two-tone chime WAV file if it does not exist."""
    if path.exists() and path.stat().st_size > 1000:
        return path
    rate = 44100
    frames = bytearray()
    for freq, length in ((880.0, 0.11), (1318.5, 0.22)):
        count = int(rate * length)
        for i in range(count):
            t = i / rate
            envelope = min(1.0, i / (rate * 0.005)) * math.exp(-5.5 * t / length)
            sample = 0.32 * envelope * (math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return path


class NotificationManager(QObject):
    activate_requested = Signal()
    failed = Signal(str)

    def __init__(self, tray: QSystemTrayIcon | None, icon: QIcon) -> None:
        super().__init__()
        self._tray = tray
        self._icon = icon
        self._queue: deque[_Pending] = deque()
        self._timer = QTimer(self)
        self._timer.setInterval(PACE_MS)
        self._timer.timeout.connect(self._deliver_next)
        self._sound_enabled = True
        self._sound = None
        self._sound_failed = False
        self._failure_reported = False
        if tray is not None:
            tray.messageClicked.connect(self.activate_requested.emit)

    @property
    def backend_name(self) -> str:
        if self._tray is not None and QSystemTrayIcon.supportsMessages():
            return "system tray"
        return "plyer"

    def set_sound_enabled(self, enabled: bool) -> None:
        self._sound_enabled = enabled

    def notify_members(self, records: list[MemberRecord], max_individual: int) -> None:
        """Queue one notification per member (up to ``max_individual``, then a summary)."""
        if not records:
            return
        individual = records[:max_individual]
        for record in individual:
            joined = format_long_date(record.joined_at)
            name = record.username
            if record.shows_display_name:
                name = f"{record.username} ({record.display_name})"
            body = f"Username: {name}\nJoined: {joined}\nChannels: {record.channel_count}"
            self._queue.append(_Pending(f"{APP_NAME} · New member found", body))
        remaining = len(records) - len(individual)
        if remaining > 0:
            self._queue.append(
                _Pending(
                    f"{APP_NAME} · {plural(remaining, 'more member')} found",
                    f"This scan also discovered {plural(remaining, 'other qualifying member')}. "
                    "Open the app to review them.",
                )
            )
        if not self._timer.isActive():
            self._deliver_next()
            self._timer.start()

    def send_test(self) -> None:
        self._queue.append(_Pending(APP_NAME, "Desktop notifications are working."))
        if not self._timer.isActive():
            self._deliver_next()
            self._timer.start()

    def clear(self) -> None:
        self._queue.clear()
        self._timer.stop()

    # --------------------------------------------------------------- internal

    def _deliver_next(self) -> None:
        if not self._queue:
            self._timer.stop()
            return
        item = self._queue.popleft()
        delivered = self._show_tray(item) or self._show_plyer(item)
        if not delivered and not self._failure_reported:
            self._failure_reported = True
            self.failed.emit("Desktop notifications are unavailable on this system (no tray or notification service).")
        if delivered and self._sound_enabled:
            self._play_sound()

    def _show_tray(self, item: _Pending) -> bool:
        if self._tray is None or not QSystemTrayIcon.supportsMessages():
            return False
        try:
            if not self._tray.isVisible():
                self._tray.show()
            self._tray.showMessage(item.title, item.body, self._icon, MESSAGE_DURATION_MS)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Tray notification failed: %s", exc)
            return False

    def _show_plyer(self, item: _Pending) -> bool:
        try:
            from plyer import notification as plyer_notification
        except Exception:  # noqa: BLE001
            return False

        def worker() -> None:
            try:
                plyer_notification.notify(title=item.title, message=item.body, app_name=APP_NAME, timeout=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("plyer notification failed: %s", exc)
                if self._failure_reported:
                    return
                self._failure_reported = True
                try:
                    self.failed.emit(f"Could not show a desktop notification: {exc}")
                except RuntimeError:
                    pass  # manager already destroyed during shutdown

        threading.Thread(target=worker, name="notify", daemon=True).start()
        return True

    def _play_sound(self) -> None:
        if self._sound_failed:
            QApplication.beep()
            return
        try:
            if self._sound is None:
                from PySide6.QtMultimedia import QSoundEffect

                path = _ensure_chime(data_dir() / "notify.wav")
                effect = QSoundEffect(self)
                effect.setSource(QUrl.fromLocalFile(str(path)))
                effect.setVolume(0.6)
                self._sound = effect
            self._sound.play()
        except Exception as exc:  # noqa: BLE001 - QtMultimedia backends are optional
            log.info("Falling back to system beep for notification sound: %s", exc)
            self._sound_failed = True
            QApplication.beep()
