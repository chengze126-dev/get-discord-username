"""Application controller: wires storage, Discord, scanner and notifications.

The controller is a QObject living on the GUI thread. All Discord and scan
work runs as asyncio tasks on the same (qasync-driven) loop and reports back
through Qt signals, so the UI never touches Discord objects directly and is
never updated from a foreign thread.
"""

from __future__ import annotations

import asyncio
import logging
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

from app.avatars import AvatarCache
from app.config import Settings, SettingsStore, TokenStore
from app.database import Database, DatabaseError
from app.discord_service import DiscordService, GuildInfo, test_connection
from app.models import ERROR_TITLES, ActivityEvent, ConnectionState, ErrorKind, MemberRecord, ScanResult
from app.notifications import NotificationManager
from app.scanner import Scanner
from app.utils import local_midnight_utc, utcnow

log = logging.getLogger(__name__)


class AppController(QObject):
    members_changed = Signal(list)              # list[MemberRecord] (full current set)
    activity_added = Signal(object)             # ActivityEvent
    toast = Signal(str, str, str)               # kind, title, body
    stats_changed = Signal()
    connection_changed = Signal(str, str)       # ConnectionState value, detail
    guild_changed = Signal(object)              # GuildInfo | None
    scan_state_changed = Signal(bool)           # scanning?
    paused_changed = Signal(bool)
    settings_changed = Signal(object)           # Settings
    test_result = Signal(object)                # ConnectionTestResult
    activate_window = Signal()

    def __init__(self, db: Database, tray: QSystemTrayIcon | None, icon: QIcon) -> None:
        super().__init__()
        self.db = db
        self.settings_store = SettingsStore(db)
        self.tokens = TokenStore()
        self.settings: Settings = self.settings_store.load()
        self.avatars = AvatarCache()
        self.notifier = NotificationManager(tray, icon)
        self.notifier.set_sound_enabled(self.settings.notification_sound)
        self.notifier.activate_requested.connect(self.activate_window.emit)
        # Bound QObject method => queued onto the GUI thread even if emitted by the plyer worker thread.
        self.notifier.failed.connect(self._on_notification_failed)

        self.discord = DiscordService(self.tokens.get, self.settings.guild_id)
        self.discord.state_changed.connect(self._on_connection_state)
        self.discord.guild_changed.connect(self._on_guild_changed)
        self.discord.error_occurred.connect(self._report_error)

        self.scanner = Scanner(db, self.discord, self.settings)
        self.scanner.scan_started.connect(lambda _manual: self.scan_state_changed.emit(True))
        self.scanner.scan_finished.connect(self._on_scan_finished)
        self.scanner.activity.connect(self.log_activity)
        self.scanner.error_occurred.connect(self._report_error)
        self.scanner.paused_changed.connect(self.paused_changed.emit)

        self.guild_info: GuildInfo | None = None
        self.connection_state = ConnectionState.DISCONNECTED
        self.connection_detail = ""
        self.last_scan: ScanResult | None = None
        self._last_connection_log: tuple[str, str] | None = None
        self._stats_cache: dict[str, int] = {"qualifying": 0, "today": 0}
        self._stats_at = 0.0
        self._tasks: set[asyncio.Task] = set()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.log_activity("info", "Discord Member Scout started")
        self.reload_members()
        self.discord.start()
        self.scanner.start()

    async def shutdown(self) -> None:
        log.info("Shutting down…")
        self.notifier.clear()
        for task in list(self._tasks):
            task.cancel()
        await self.scanner.stop()
        await self.discord.stop()
        await self.avatars.close()
        try:
            self.db.add_activity("info", "Application closed")
        except DatabaseError:
            pass
        self.db.close()
        log.info("Shutdown complete")

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ data

    def reload_members(self) -> list[MemberRecord]:
        records: list[MemberRecord] = []
        if self.settings.guild_id:
            try:
                records = self.db.load_members(self.settings.guild_id, self.settings.cutoff)
            except DatabaseError as exc:
                self._report_error(ErrorKind.DATABASE.value, str(exc))
        self.members_changed.emit(records)
        self.refresh_stats(force=True)
        return records

    def recent_activity(self) -> list[ActivityEvent]:
        try:
            return self.db.recent_activity()
        except DatabaseError as exc:
            log.error("Could not load activity: %s", exc)
            return []

    def recent_scans(self):
        try:
            return self.db.recent_scans(50)
        except DatabaseError:
            return []

    def refresh_stats(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._stats_at < 30:
            return
        self._stats_at = time.monotonic()
        gid = self.settings.guild_id
        if gid:
            try:
                self._stats_cache = {
                    "qualifying": self.db.count_qualifying(gid, self.settings.cutoff),
                    "today": self.db.count_discovered_since(gid, local_midnight_utc(), self.settings.cutoff),
                }
            except DatabaseError as exc:
                log.error("Stats query failed: %s", exc)
        else:
            self._stats_cache = {"qualifying": 0, "today": 0}
        self.stats_changed.emit()

    @property
    def qualifying_count(self) -> int:
        return self._stats_cache["qualifying"]

    @property
    def discovered_today(self) -> int:
        return self._stats_cache["today"]

    @property
    def members_scanned(self) -> int | None:
        if self.last_scan is not None and self.last_scan.status == "success":
            return self.last_scan.members_checked
        if self.settings.guild_id:
            try:
                row = self.db.last_successful_scan(self.settings.guild_id)
                return int(row["members_checked"]) if row else None
            except DatabaseError:
                return None
        return None

    def log_activity(self, event_type: str, message: str) -> None:
        try:
            event = self.db.add_activity(event_type, message)
        except DatabaseError as exc:
            log.error("Could not write activity log: %s", exc)
            event = ActivityEvent(0, utcnow(), event_type, message)
        self.activity_added.emit(event)

    # --------------------------------------------------------------- actions

    def scan_now(self) -> None:
        if not self.discord.is_ready:
            self.toast.emit("warning", "Not connected", "Discord is not connected to the configured guild yet.")
            return
        if not self.scanner.request_scan():
            self.toast.emit("info", "Scan already in progress", "Please wait a few seconds before scanning again.")

    def toggle_pause(self) -> None:
        if self.scanner.paused:
            self.scanner.resume()
        else:
            self.scanner.pause()

    def request_test(self, token: str, guild_id: int | None) -> None:
        async def run() -> None:
            result = await test_connection(token or (self.tokens.get() or ""), guild_id)
            self.test_result.emit(result)

        self._spawn(run())

    def send_test_notification(self) -> None:
        self.notifier.set_sound_enabled(self.settings.notification_sound)
        self.notifier.send_test()

    def clear_token(self) -> None:
        self.tokens.clear()
        self.log_activity("info", "Stored bot token removed")
        self.toast.emit("info", "Token removed", "The bot token was removed from the credential store.")
        self.settings_changed.emit(self.settings)
        self._spawn(self.discord.restart())

    def apply_settings(self, new: Settings, new_token: str | None) -> None:
        old = self.settings
        try:
            self.settings_store.save(new)
        except DatabaseError as exc:
            self._report_error(ErrorKind.DATABASE.value, f"Could not save settings: {exc}")
            return
        self.settings = new
        self.scanner.update_settings(new)
        self.notifier.set_sound_enabled(new.notification_sound)

        reconnect = False
        if new_token:
            persisted = self.tokens.set(new_token)
            reconnect = True
            if not persisted:
                self.toast.emit(
                    "warning",
                    "Token kept for this session only",
                    "No OS credential store is available. Set DISCORD_BOT_TOKEN to persist it.",
                )
        if new.guild_id != old.guild_id:
            self.discord.set_guild_id(new.guild_id)
        if new.guild_id != old.guild_id or new.cutoff != old.cutoff:
            self.reload_members()
        if reconnect:
            self._spawn(self.discord.restart())

        self.log_activity("info", "Settings saved")
        self.toast.emit("success", "Settings saved", "")
        self.settings_changed.emit(new)

    # -------------------------------------------------------------- handlers

    def _report_error(self, kind_value: str, message: str) -> None:
        try:
            kind = ErrorKind(kind_value)
        except ValueError:
            kind = ErrorKind.UNKNOWN
        title = ERROR_TITLES.get(kind, "Error")
        self.log_activity("error", f"{title}: {message}")
        self.toast.emit("warning" if kind == ErrorKind.RATE_LIMITED else "error", title, message)

    def _on_notification_failed(self, message: str) -> None:
        self._report_error(ErrorKind.NOTIFICATION.value, message)

    def _on_connection_state(self, state_value: str, detail: str) -> None:
        state = ConnectionState(state_value)
        self.connection_state = state
        self.connection_detail = detail
        self.connection_changed.emit(state_value, detail)
        key = (state_value, detail)
        if key == self._last_connection_log:
            return
        self._last_connection_log = key
        if state == ConnectionState.CONNECTED:
            self.log_activity("connection", f"Connected to {detail}")
        elif state == ConnectionState.DISCONNECTED and detail and not detail.startswith("Reconnecting in"):
            self.log_activity("connection", f"Disconnected - {detail}")

    def _on_guild_changed(self, info: GuildInfo | None) -> None:
        self.guild_info = info
        self.guild_changed.emit(info)

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.last_scan = result
        self.scan_state_changed.emit(False)
        if result.status != "success":
            self.stats_changed.emit()
            return
        self.reload_members()
        self._dispatch_notifications()

    def _dispatch_notifications(self) -> None:
        gid = self.settings.guild_id
        if not gid:
            return
        try:
            pending = self.db.pending_notifications(gid, self.settings.cutoff)
            if not pending:
                return
            if self.settings.notifications_enabled:
                self.notifier.set_sound_enabled(self.settings.notification_sound)
                self.notifier.notify_members(pending, self.settings.max_notifications_per_scan)
            # Persist delivery state so restarts never notify the same member again.
            self.db.mark_notified(gid, [r.user_id for r in pending])
        except DatabaseError as exc:
            self._report_error(ErrorKind.DATABASE.value, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._report_error(ErrorKind.NOTIFICATION.value, str(exc))
