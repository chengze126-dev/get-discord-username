"""Main application window."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from app import APP_NAME
from app.controller import AppController
from app.models import ActivityEvent, ConnectionState, GuildStatus, MemberRecord, ScanResult
from app.utils import format_clock, format_countdown, format_duration_ms, format_int, format_long_date, from_iso
from ui.activity_page import ActivityPage
from ui.components.icons import app_icon
from ui.components.server_strip import ServerStrip
from ui.components.sidebar import Sidebar
from ui.components.status_indicator import STATE_LABEL
from ui.components.toast import ToastManager
from ui.dashboard import ActivityModel, DashboardPage, PageHeader
from ui.member_details import MemberDetailsDrawer
from ui.member_table import (
    COL_BOT,
    COL_CHANNELS,
    COL_DISPLAY,
    COL_ID,
    COL_JOINED,
    COL_NO,
    COL_ROLES,
    COL_USER,
    MemberSection,
    MemberTreeModel,
    default_export_name,
    export_csv,
)
from ui.settings_dialog import SettingsForm, SettingsPage
from ui.theme import ThemeManager

log = logging.getLogger(__name__)

PAGES = ["dashboard", "members", "activity", "settings"]


class MembersPage(QWidget):
    """Server cards on top, every member of the selected server(s) below, grouped by server."""

    def __init__(self, strip: ServerStrip, section: MemberSection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        self.header = PageHeader("Members by Server", "Every member of each configured server, kept separate per Guild ID")
        layout.addWidget(self.header)
        layout.addWidget(strip)
        layout.addWidget(section, 1)


class MainWindow(QMainWindow):
    close_requested = Signal()

    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1200, 750)
        self.resize(1440, 900)

        # One shared tree model; each view filters it independently.
        self.member_model = MemberTreeModel(controller.guild_state, self)
        self.activity_model = ActivityModel(self)

        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self.show_page)
        layout.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        layout.addWidget(self.pages, 1)

        avatars = controller.avatars
        self.dashboard = DashboardPage(self.member_model, self.activity_model, avatars)
        self.server_strip = ServerStrip(avatars)
        self.members_section = MemberSection(
            self.member_model,
            avatars,
            "Members",
            columns=[COL_NO, COL_USER, COL_DISPLAY, COL_ID, COL_JOINED, COL_BOT, COL_ROLES, COL_CHANNELS],
        )
        self.members_page = MembersPage(self.server_strip, self.members_section)
        self.activity_page = ActivityPage(self.activity_model)
        self.settings_page = SettingsPage()
        for page in (self.dashboard, self.members_page, self.activity_page, self.settings_page):
            self.pages.addWidget(page)

        self.toasts = ToastManager(root)
        self.drawer = MemberDetailsDrawer(root, avatars)
        self.drawer.copied.connect(lambda msg: self.toasts.show("success", msg, "", 2500))

        self._wire()
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=controller.refresh_all)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.show_page("settings"))

        self._on_guilds_changed()
        self.activity_model.set_events(controller.recent_activity())
        self._restore_last_scan()
        self._on_settings_changed(controller.settings)
        self._on_connection(controller.connection_state.value, controller.connection_detail)
        self._on_stats()
        self._tick()

    # ---------------------------------------------------------------- wiring

    def _wire(self) -> None:
        c = self.controller
        c.guild_members_changed.connect(self._on_guild_members)
        c.guilds_changed.connect(self._on_guilds_changed)
        c.guild_state_changed.connect(self._on_guild_state)
        c.activity_added.connect(self._on_activity)
        c.toast.connect(lambda kind, title, body: self.toasts.show(kind, title, body, 7000 if kind == "error" else 4500))
        c.stats_changed.connect(self._on_stats)
        c.connection_changed.connect(self._on_connection)
        c.scan_state_changed.connect(self._on_scan_state)
        c.paused_changed.connect(self._on_paused)
        c.settings_changed.connect(self._on_settings_changed)
        c.test_result.connect(self.settings_page.show_test_result)
        c.bot_guilds_result.connect(self.settings_page.show_bot_guilds)
        c.activate_window.connect(self.bring_to_front)
        c.avatars.avatar_ready.connect(self.member_model.refresh_avatar)
        c.scanner.guild_scan_finished.connect(self._on_guild_scan_finished)
        c.scanner.cycle_finished.connect(self._on_cycle_finished)

        self.dashboard.pause_toggled.connect(c.toggle_pause)
        self.dashboard.scan_now.connect(c.refresh_all)
        self.dashboard.activity.view_all.connect(lambda: self.show_page("activity"))
        for section in (self.dashboard.members, self.members_section):
            section.member_activated.connect(self._show_member)
            section.refresh_guild_requested.connect(c.refresh_guild)
            section.refresh_all_requested.connect(c.refresh_all)
            section.export_requested.connect(self._export)

        # Server cards and the server picker of the Members page stay in sync.
        self.server_strip.server_selected.connect(self.members_section.set_selected_guild)
        self.server_strip.refresh_requested.connect(c.refresh_guild)
        self.members_section.guild_selected.connect(lambda gid: self.server_strip.select(gid, emit=False))

        sp = self.settings_page
        sp.save_requested.connect(self._on_save_settings)
        sp.test_requested.connect(c.request_test)
        sp.find_servers_requested.connect(c.request_bot_guilds)
        sp.clear_token_requested.connect(c.clear_token)
        sp.test_notification_requested.connect(c.send_test_notification)
        sp.theme_preview.connect(ThemeManager.instance().apply)

    # ----------------------------------------------------------------- pages

    def show_page(self, key: str) -> None:
        if key not in PAGES:
            return
        if key == "activity":
            self.activity_page.history_model.set_rows(self.controller.recent_scans())
        if key == "settings":
            self._load_settings_page()
        self.pages.setCurrentIndex(PAGES.index(key))
        self.sidebar.set_current(key)
        self.drawer.close_drawer()

    def _load_settings_page(self) -> None:
        tokens = self.controller.tokens
        self.settings_page.load(self.controller.settings, tokens.source(), tokens.get() is not None)

    def _focus_search(self) -> None:
        index = self.pages.currentIndex()
        section = self.members_section if index == PAGES.index("members") else self.dashboard.members
        if index not in (PAGES.index("members"), PAGES.index("dashboard")):
            self.show_page("dashboard")
        section.search.setFocus()
        section.search.selectAll()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    # --------------------------------------------------------------- handlers

    def _on_guilds_changed(self) -> None:
        states = self.controller.guild_list()
        self.member_model.set_guilds([s.guild_id for s in states])
        self.server_strip.set_guilds(states)
        self.dashboard.members.set_guilds(states)
        self.members_section.set_guilds(states)
        self._update_server_summary()

    def _on_guild_members(self, guild_id: int, records: list[MemberRecord]) -> None:
        self.member_model.set_members(guild_id, records)
        if self.drawer.is_open and self.drawer.current_key is not None:
            gid, uid = self.drawer.current_key
            if gid == guild_id:
                for record in records:
                    if record.user_id == uid:
                        self.drawer.refresh(record)
                        break

    def _on_guild_state(self, guild_id: int) -> None:
        state = self.controller.guild_state(guild_id)
        if state is None:
            return
        self.member_model.refresh_guild(guild_id)
        self.server_strip.update_guild(state)
        self.dashboard.members.update_guild_name(state)
        self.members_section.update_guild_name(state)
        self._update_server_summary()

    def _update_server_summary(self) -> None:
        states = self.controller.guild_list()
        available = [
            s for s in states if s.status not in (GuildStatus.PENDING, GuildStatus.NOT_MEMBER, GuildStatus.UNAVAILABLE)
        ]
        members = sum(s.member_count or 0 for s in available)
        if states:
            label = f"{len(available)} of {len(states)} server{'s' if len(states) != 1 else ''}"
            self.sidebar.connection.set_guild(label, members if available else None)
        else:
            self.sidebar.connection.set_guild(None, None)
        self.dashboard.activity.status.set_value("servers", f"{len(available)} / {len(states)} available")

    def _on_activity(self, event: ActivityEvent) -> None:
        self.activity_model.add_event(event)

    def _on_stats(self) -> None:
        c = self.controller
        count = len(c.settings.guild_ids)
        servers = f"across {count} server{'s' if count != 1 else ''}"
        self.dashboard.card_qualifying.set_value(format_int(c.qualifying_count))
        self.dashboard.card_qualifying.set_caption(f"Joined before {format_long_date(c.settings.cutoff)}, {servers}")
        self.dashboard.card_today.set_value(format_int(c.discovered_today))
        self.dashboard.card_today.set_caption("New since midnight")
        self.dashboard.card_scanned.set_value(format_int(c.members_scanned))
        self.dashboard.card_scanned.set_caption(servers + (" (bots ignored)" if c.settings.ignore_bots else ""))

    def _on_guild_scan_finished(self, result: ScanResult) -> None:
        if result.status == "success" and result.manual:
            self.toasts.show(
                "success",
                f"{result.guild_name} synced",
                f"{result.members_loaded:,} members loaded · {result.matches_found:,} joined before cutoff"
                + (f" · {result.new_matches:,} new" if result.new_matches else ""),
                3500,
            )

    def _on_cycle_finished(self, results: list[ScanResult]) -> None:
        done = [r for r in results if r.status != "skipped"]
        panel = self.dashboard.activity.status
        if done:
            panel.set_value("last", format_clock(max(r.completed_at for r in done)))
            panel.set_value("duration", format_duration_ms(sum(r.duration_ms for r in done)))
        ok = [r for r in self.controller.scanner.last_results.values() if r.status == "success"]
        if ok:
            panel.set_value("checked", format_int(sum(r.members_checked for r in ok)))
            panel.set_value("matches", format_int(sum(r.matches_found for r in ok)))
        if any(r.status == "error" for r in done):
            panel.set_value("matches", panel._values["matches"].text() + "  (errors)")
        if self.pages.currentIndex() == PAGES.index("activity"):
            self.activity_page.history_model.set_rows(self.controller.recent_scans())

    def _restore_last_scan(self) -> None:
        """Show the most recent persisted sync in the Scanner panel after a restart."""
        rows = [r for r in self.controller.recent_scans() if r["status"] == "success"]
        if not rows:
            return
        panel = self.dashboard.activity.status
        latest = {}
        for row in rows:  # newest first: keep the latest row per server
            latest.setdefault(row["guild_id"], row)
        configured = {str(g) for g in self.controller.settings.guild_ids}
        latest = {g: r for g, r in latest.items() if g in configured}
        if not latest:
            return
        panel.set_value("last", format_clock(from_iso(rows[0]["completed_at"])))
        panel.set_value("duration", format_duration_ms(rows[0]["duration_ms"]))
        panel.set_value("checked", format_int(sum(r["members_checked"] for r in latest.values())))
        panel.set_value("matches", format_int(sum(r["matches_found"] for r in latest.values())))

    def _on_connection(self, state_value: str, detail: str) -> None:
        state = ConnectionState(state_value)
        self.sidebar.connection.set_state(state, detail)
        self.dashboard.activity.status.set_value("connection", STATE_LABEL[state])
        self.dashboard.activity.status._values["connection"].setToolTip(detail)
        self._update_server_summary()
        self._tick()

    def _on_scan_state(self, scanning: bool) -> None:
        self.dashboard.set_scanning(scanning)
        self._tick()

    def _on_paused(self, paused: bool) -> None:
        self.dashboard.set_paused(paused)
        self.toasts.show("info", "Monitoring paused" if paused else "Monitoring resumed", "", 2500)
        self._tick()

    def _on_settings_changed(self, settings) -> None:
        cutoff = settings.cutoff
        short = cutoff.strftime("%b ") + str(cutoff.day) + cutoff.strftime(", %Y")
        count = len(settings.guild_ids)
        self.dashboard.header.subtitle.setText(
            f"Monitoring {count} server{'s' if count != 1 else ''} for members who joined before {short}"
        )
        self.dashboard.members.set_cutoff(settings.cutoff, settings.ignore_bots)
        if ThemeManager.instance().mode != settings.theme:
            ThemeManager.instance().apply(settings.theme)
        if self.pages.currentIndex() == PAGES.index("settings"):
            self._load_settings_page()
        self._on_stats()

    def _on_save_settings(self, form: SettingsForm) -> None:
        self.controller.apply_settings(form.settings, form.new_token)

    def _tick(self) -> None:
        c = self.controller
        scanner = c.scanner
        if scanner.is_scanning:
            gid = scanner.scanning_guild
            state = c.guild_state(gid) if gid is not None else None
            countdown = "Syncing"
            caption = f"Syncing {state.display_name}…" if state else "Sync in progress…"
        elif scanner.paused:
            countdown, caption = "Paused", "Monitoring paused"
        elif not c.discord.is_ready:
            countdown = "--:--"
            caption = "Waiting for Discord connection"
        elif not c.settings.guild_ids:
            countdown, caption = "--:--", "Add servers in Settings"
        else:
            remaining = scanner.seconds_until_next()
            countdown = format_countdown(remaining)
            caption = f"Next sync in {countdown} · every {c.settings.scan_interval}s"
        self.dashboard.card_next.set_value(countdown)
        self.dashboard.card_next.set_caption(caption)
        self.dashboard.activity.status.set_value("next", countdown)
        c.refresh_stats()  # throttled internally (keeps "Discovered Today" correct across midnight)

    # ----------------------------------------------------------------- export

    def _export(self, groups, scope: str) -> None:
        total = sum(len(records) for _state, records in groups)
        if not total:
            self.toasts.show("info", "Nothing to export", "No members match the current filters.")
            return
        cutoff = self.controller.settings.cutoff if self.sender() is self.dashboard.members else None
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
        suggested = str(Path(docs) / default_export_name(scope, cutoff))
        path, _ = QFileDialog.getSaveFileName(self, "Export members to CSV", suggested, "CSV files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            count = export_csv(groups, Path(path))
        except OSError as exc:
            self.toasts.show("error", "Export failed", str(exc))
            self.controller.log_activity("error", f"CSV export failed: {exc}")
            return
        servers = len([g for g in groups if g[1]])
        self.toasts.show("success", "Export complete", f"{count:,} members from {servers} server(s) written to {Path(path).name}")
        self.controller.log_activity("info", f"Exported {count:,} members to {Path(path).name}")

    # ------------------------------------------------------------------ misc

    def _show_member(self, record: MemberRecord) -> None:
        self.drawer.show_member(record)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.drawer.is_open:
            self.drawer.close_drawer()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._tick_timer.stop()
        self.close_requested.emit()
        event.accept()
