"""Application controller: wires storage, Discord, scanner and notifications.

The controller is a QObject living on the GUI thread. All Discord and scan
work runs as asyncio tasks on the same (qasync-driven) loop and reports back
through Qt signals, so the UI never touches Discord objects directly and is
never updated from a foreign thread.

Everything member-related is keyed by guild ID: per-server state lives in
``guild_states`` and member lists are emitted per server
(``guild_members_changed(guild_id, records)``), so the UI can never mix up
members of different servers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

from app.avatars import AvatarCache
from app.config import (
    GUILD_IDS_ENV_KEY,
    LEGACY_GUILD_ENV_KEY,
    Settings,
    SettingsStore,
    TokenStore,
    format_guild_ids,
    write_env_value,
)
from app.database import Database, DatabaseError
from app.discord_service import DiscordService, GuildInfo, describe_exception, list_bot_guilds, test_connection
from app.models import (
    ERROR_TITLES,
    ActivityEvent,
    ConnectionState,
    ErrorKind,
    GuildState,
    GuildStatus,
    ScanResult,
)
from app.notifications import NotificationManager
from app.scanner import Scanner
from app.utils import local_midnight_utc, utcnow

log = logging.getLogger(__name__)


class AppController(QObject):
    guild_members_changed = Signal(object, list)   # guild id, list[MemberRecord] (that server only)
    guilds_changed = Signal()                   # configured server list changed
    guild_state_changed = Signal(object)           # one server's card/header needs repainting
    activity_added = Signal(object)             # ActivityEvent
    toast = Signal(str, str, str)               # kind, title, body
    stats_changed = Signal()
    connection_changed = Signal(str, str)       # ConnectionState value, detail
    scan_state_changed = Signal(bool)           # scanning?
    paused_changed = Signal(bool)
    settings_changed = Signal(object)           # Settings
    test_result = Signal(object)                # ConnectionTestResult
    bot_guilds_result = Signal(object)          # BotGuildList or error string
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

        self.guild_states: dict[int, GuildState] = {}
        self._rebuild_guild_states()

        self.discord = DiscordService(self.tokens.get, self.settings.guild_ids)
        self.discord.state_changed.connect(self._on_connection_state)
        self.discord.guild_status_changed.connect(self._on_guild_status)
        self.discord.error_occurred.connect(self._report_error)

        self.scanner = Scanner(db, self.discord, self.settings)
        self.scanner.cycle_started.connect(lambda _manual: self.scan_state_changed.emit(True))
        self.scanner.cycle_finished.connect(self._on_cycle_finished)
        self.scanner.guild_scan_started.connect(self._on_guild_scan_started)
        self.scanner.guild_progress.connect(self._on_guild_progress)
        self.scanner.guild_scan_finished.connect(self._on_guild_scan_finished)
        self.scanner.activity.connect(self.log_activity)
        self.scanner.error_occurred.connect(self._report_error)
        self.scanner.paused_changed.connect(self.paused_changed.emit)

        self.connection_state = ConnectionState.DISCONNECTED
        self.connection_detail = ""
        self._last_connection_log: tuple[str, str] | None = None
        self._stats_cache: dict[str, int] = {"qualifying": 0, "today": 0, "scanned": 0}
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

    # ---------------------------------------------------------------- guilds

    def guild_list(self) -> list[GuildState]:
        """Configured servers, in the order the user entered them."""
        return [self.guild_states[g] for g in self.settings.guild_ids if g in self.guild_states]

    def guild_state(self, guild_id: int) -> GuildState | None:
        return self.guild_states.get(guild_id)

    def _rebuild_guild_states(self) -> None:
        ids = self.settings.guild_ids
        try:
            stored = self.db.get_guilds(ids)
        except DatabaseError as exc:
            log.error("Could not load server info: %s", exc)
            stored = {}
        states: dict[int, GuildState] = {}
        for gid in ids:
            state = self.guild_states.get(gid) or GuildState(guild_id=gid)
            info = stored.get(gid, {})
            state.name = state.name or info.get("name", "")
            state.member_count = state.member_count or info.get("member_count")
            state.icon_url = state.icon_url or info.get("icon_url")
            state.last_synced_at = state.last_synced_at or info.get("last_synced_at")
            if state.status == GuildStatus.PENDING and info.get("last_status") == "error":
                state.message = info.get("last_error") or ""
            states[gid] = state
            self._refresh_counts(state)
        self.guild_states = states

    def _refresh_counts(self, state: GuildState) -> None:
        try:
            state.loaded = self.db.count_members(state.guild_id)
            state.qualifying = self.db.count_members(state.guild_id, self.settings.cutoff, self.settings.ignore_bots)
        except DatabaseError as exc:
            log.error("Count query failed: %s", exc)

    # ------------------------------------------------------------------ data

    def reload_members(self, guild_id: int | None = None) -> None:
        """Emit the stored member directory of one server (or of every configured server)."""
        guild_ids = [guild_id] if guild_id is not None else list(self.settings.guild_ids)
        for gid in guild_ids:
            if gid not in self.guild_states:
                continue
            try:
                records = self.db.load_members(gid)
            except DatabaseError as exc:
                self._report_error(ErrorKind.DATABASE.value, str(exc))
                records = []
            state = self.guild_states[gid]
            for record in records:
                if not record.guild_name:
                    record.guild_name = state.display_name
            self._refresh_counts(state)
            self.guild_members_changed.emit(gid, records)
            self.guild_state_changed.emit(gid)
        self.refresh_stats(force=True)

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
        ids = list(self.settings.guild_ids)
        qualifying = sum(s.qualifying for s in self.guild_list())
        try:
            today = self.db.count_discovered_since(ids, local_midnight_utc(), self.settings.cutoff, self.settings.ignore_bots)
        except DatabaseError as exc:
            log.error("Stats query failed: %s", exc)
            today = self._stats_cache.get("today", 0)
        scanned = 0
        scanner = getattr(self, "scanner", None)
        for gid in ids:
            result = scanner.last_results.get(gid) if scanner is not None else None
            if result is not None and result.status == "success":
                scanned += result.members_checked
                continue
            try:
                row = self.db.last_successful_scan(gid)
                scanned += int(row["members_checked"]) if row else 0
            except DatabaseError:
                pass
        self._stats_cache = {"qualifying": qualifying, "today": today, "scanned": scanned}
        self.stats_changed.emit()

    @property
    def qualifying_count(self) -> int:
        return self._stats_cache["qualifying"]

    @property
    def discovered_today(self) -> int:
        return self._stats_cache["today"]

    @property
    def members_scanned(self) -> int:
        return self._stats_cache["scanned"]

    def log_activity(self, event_type: str, message: str) -> None:
        try:
            event = self.db.add_activity(event_type, message)
        except DatabaseError as exc:
            log.error("Could not write activity log: %s", exc)
            event = ActivityEvent(0, utcnow(), event_type, message)
        self.activity_added.emit(event)

    # --------------------------------------------------------------- actions

    def scan_now(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        if not self._can_refresh():
            return
        if not self.scanner.request_scan(None):
            self.toast.emit("info", "Refresh already requested", "Please wait a few seconds before refreshing again.")

    def refresh_guild(self, guild_id: int) -> None:
        state = self.guild_states.get(guild_id)
        if state is None or not self._can_refresh():
            return
        if self.discord.guild(guild_id) is None:
            self.toast.emit("warning", "Cannot refresh this server", state.message or "The bot is not in this server.")
            return
        if not self.scanner.request_scan([guild_id]):
            self.toast.emit("info", "Refresh already requested", f"{state.display_name} is being refreshed.")

    def _can_refresh(self) -> bool:
        if not self.settings.guild_ids:
            self.toast.emit("warning", "No servers configured", "Add Server (Guild) IDs in Settings first.")
            return False
        if not self.discord.is_ready:
            self.toast.emit("warning", "Not connected", "Discord is not connected yet.")
            return False
        return True

    def toggle_pause(self) -> None:
        if self.scanner.paused:
            self.scanner.resume()
        else:
            self.scanner.pause()

    def request_test(self, token: str, guild_ids: tuple[int, ...]) -> None:
        async def run() -> None:
            result = await test_connection(token or (self.tokens.get() or ""), tuple(guild_ids))
            self.test_result.emit(result)

        self._spawn(run())

    def request_bot_guilds(self, token: str) -> None:
        """List the servers the bot has been invited to (for the Settings server picker)."""

        async def run() -> None:
            token_value = token or (self.tokens.get() or "")
            if not token_value:
                self.bot_guilds_result.emit("Enter the bot token first.")
                return
            try:
                self.bot_guilds_result.emit(await list_bot_guilds(token_value))
            except Exception as exc:  # noqa: BLE001
                self.bot_guilds_result.emit(describe_exception(exc)[1])

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
        self._sync_guilds_to_env(new.guild_ids)
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

        if new.guild_ids != old.guild_ids:
            added = [g for g in new.guild_ids if g not in old.guild_ids]
            self._rebuild_guild_states()
            self.discord.set_guild_ids(new.guild_ids)
            self.guilds_changed.emit()
            self.reload_members()
            if added and self.discord.is_ready and not reconnect:
                self.scanner.request_scan(added)
        elif new.cutoff != old.cutoff or new.ignore_bots != old.ignore_bots:
            for state in self.guild_list():
                self._refresh_counts(state)
                self.guild_state_changed.emit(state.guild_id)
            self.refresh_stats(force=True)
        if reconnect:
            self._spawn(self.discord.restart())

        self.log_activity("info", "Settings saved")
        self.toast.emit("success", "Settings saved", "")
        self.settings_changed.emit(new)

    def _sync_guilds_to_env(self, guild_ids: tuple[int, ...]) -> None:
        """Mirror the server list into .env so the file and the Settings page always agree."""
        value = format_guild_ids(guild_ids)
        legacy = os.environ.get(LEGACY_GUILD_ENV_KEY, "").strip()
        if os.environ.get(GUILD_IDS_ENV_KEY, "").strip() == value and not legacy:
            return
        try:
            write_env_value(GUILD_IDS_ENV_KEY, value)
            if legacy:
                write_env_value(LEGACY_GUILD_ENV_KEY, "")  # superseded by DISCORD_GUILD_IDS
            self.log_activity("info", f"Updated {GUILD_IDS_ENV_KEY} in .env")
        except OSError as exc:
            self.toast.emit(
                "warning",
                "Could not update .env",
                f"The servers were saved in the app, but .env could not be written: {exc}",
            )

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
            self.log_activity("connection", f"Connected - {detail}")
        elif state == ConnectionState.DISCONNECTED and detail and not detail.startswith("Reconnecting in"):
            self.log_activity("connection", f"Disconnected - {detail}")

    def _on_guild_status(self, guild_id: int, status_value: str, message: str, info: GuildInfo | None) -> None:
        state = self.guild_states.get(guild_id)
        if state is None:
            return
        status = GuildStatus(status_value)
        if status == GuildStatus.AVAILABLE and info is not None:
            renamed = state.name != info.name
            state.name = info.name
            state.member_count = info.member_count
            state.icon_url = info.icon_url or state.icon_url
            try:
                self.db.upsert_guild(guild_id, info.name, info.member_count, info.icon_url)
            except DatabaseError as exc:
                log.error("Could not store server info: %s", exc)
            if state.status != GuildStatus.LOADING:
                state.status = GuildStatus.SYNCED if state.last_synced_at else GuildStatus.AVAILABLE
                state.message = ""
            if renamed:
                self.reload_members(guild_id)  # refresh the server name shown on its members
                return
        elif status != GuildStatus.AVAILABLE:
            state.status = status
            state.message = message
            state.progress = None
        self.guild_state_changed.emit(guild_id)

    def _on_guild_scan_started(self, guild_id: int) -> None:
        state = self.guild_states.get(guild_id)
        if state is None:
            return
        state.status = GuildStatus.LOADING
        state.progress = 0.0
        state.message = "Loading members…"
        self.guild_state_changed.emit(guild_id)

    def _on_guild_progress(self, guild_id: int, loaded: int, total: int) -> None:
        state = self.guild_states.get(guild_id)
        if state is None:
            return
        state.progress = min(1.0, loaded / total) if total else None
        state.message = f"Loading {loaded:,} / {total:,}" if total else f"Loading {loaded:,}"
        self.guild_state_changed.emit(guild_id)

    def _on_guild_scan_finished(self, result: ScanResult) -> None:
        gid = result.guild_id
        state = self.guild_states.get(gid) if gid is not None else None
        if state is None or result.status == "skipped":
            return
        state.progress = None
        if result.status == "success":
            state.status = GuildStatus.SYNCED
            state.message = ""
            state.last_synced_at = result.completed_at
            try:
                self.db.set_guild_sync_state(gid, "success", None, result.completed_at)
            except DatabaseError:
                pass
            self.reload_members(gid)
            self._dispatch_notifications()
        else:
            state.status = GuildStatus.ERROR
            state.message = result.error_message or "Sync failed"
            try:
                self.db.set_guild_sync_state(gid, "error", state.message, None)
            except DatabaseError:
                pass
            self.guild_state_changed.emit(gid)

    def _on_cycle_finished(self, _results: list[ScanResult]) -> None:
        self.scan_state_changed.emit(False)
        self.refresh_stats(force=True)

    def _dispatch_notifications(self) -> None:
        ids = list(self.settings.guild_ids)
        if not ids:
            return
        try:
            pending = self.db.pending_notifications(ids, self.settings.cutoff, self.settings.ignore_bots)
            if not pending:
                return
            for record in pending:
                state = self.guild_states.get(record.guild_id)
                if state is not None and not record.guild_name:
                    record.guild_name = state.display_name
            if self.settings.notifications_enabled:
                self.notifier.set_sound_enabled(self.settings.notification_sound)
                self.notifier.notify_members(pending, self.settings.max_notifications_per_scan)
            # Persist delivery state so restarts never notify the same member again.
            self.db.mark_notified(pending)
        except DatabaseError as exc:
            self._report_error(ErrorKind.DATABASE.value, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._report_error(ErrorKind.NOTIFICATION.value, str(exc))
