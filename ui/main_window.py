"""Main application window."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from app import APP_NAME
from app.controller import AppController
from app.discord_service import GuildInfo
from app.models import ActivityEvent, ConnectionState, MemberRecord, ScanResult
from app.utils import format_clock, format_countdown, format_duration_ms, format_int, format_long_date, from_iso
from ui.activity_page import ActivityPage
from ui.components.icons import app_icon
from ui.components.sidebar import Sidebar
from ui.components.status_indicator import STATE_LABEL
from ui.components.toast import ToastManager
from ui.dashboard import ActivityModel, DashboardPage, PageHeader
from ui.member_details import MemberDetailsDrawer
from ui.member_table import MemberTableModel, MemberTableSection, default_export_name, export_csv
from ui.settings_dialog import SettingsForm, SettingsPage
from ui.theme import ThemeManager

log = logging.getLogger(__name__)

PAGES = ["dashboard", "members", "activity", "settings"]


class MembersPage(QWidget):
    def __init__(self, section: MemberTableSection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)
        self.header = PageHeader("Members", "Every stored member who joined before the cutoff date")
        layout.addWidget(self.header)
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

        self.member_model = MemberTableModel(self)
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
        self.members_section = MemberTableSection(self.member_model, avatars, "All Members")
        self.members_page = MembersPage(self.members_section)
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
        QShortcut(QKeySequence("Ctrl+R"), self, activated=controller.scan_now)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.show_page("settings"))

        self.activity_model.set_events(controller.recent_activity())
        self._restore_last_scan()
        self._on_settings_changed(controller.settings)
        self._on_connection(controller.connection_state.value, controller.connection_detail)
        self._on_stats()
        self._tick()

    # ---------------------------------------------------------------- wiring

    def _wire(self) -> None:
        c = self.controller
        c.members_changed.connect(self._on_members)
        c.activity_added.connect(self._on_activity)
        c.toast.connect(lambda kind, title, body: self.toasts.show(kind, title, body, 7000 if kind == "error" else 4500))
        c.stats_changed.connect(self._on_stats)
        c.connection_changed.connect(self._on_connection)
        c.guild_changed.connect(self._on_guild)
        c.scan_state_changed.connect(self._on_scan_state)
        c.paused_changed.connect(self._on_paused)
        c.settings_changed.connect(self._on_settings_changed)
        c.test_result.connect(self.settings_page.show_test_result)
        c.activate_window.connect(self.bring_to_front)
        c.avatars.avatar_ready.connect(self.member_model.refresh_avatar)
        c.scanner.scan_finished.connect(self._on_scan_finished)

        self.dashboard.pause_toggled.connect(c.toggle_pause)
        self.dashboard.scan_now.connect(c.scan_now)
        self.dashboard.activity.view_all.connect(lambda: self.show_page("activity"))
        for section in (self.dashboard.members, self.members_section):
            section.member_activated.connect(self._show_member)
            section.refresh_requested.connect(self._on_refresh)
            section.export_requested.connect(self._export)

        sp = self.settings_page
        sp.save_requested.connect(self._on_save_settings)
        sp.test_requested.connect(c.request_test)
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

    def _on_members(self, records: list[MemberRecord]) -> None:
        self.member_model.sync(records)
        if self.drawer.is_open and self.drawer.current_user_id is not None:
            for record in records:
                if record.user_id == self.drawer.current_user_id:
                    self.drawer.refresh(record)
                    break

    def _on_activity(self, event: ActivityEvent) -> None:
        self.activity_model.add_event(event)

    def _on_refresh(self) -> None:
        self.controller.reload_members()
        self.toasts.show("info", "Table refreshed", f"{self.member_model.rowCount():,} members loaded", 2500)

    def _on_stats(self) -> None:
        c = self.controller
        self.dashboard.card_qualifying.set_value(format_int(c.qualifying_count))
        self.dashboard.card_qualifying.set_caption(f"Joined before {format_long_date(c.settings.cutoff)}")
        self.dashboard.card_today.set_value(format_int(c.discovered_today))
        self.dashboard.card_today.set_caption("New since midnight")
        scanned = c.members_scanned
        self.dashboard.card_scanned.set_value(format_int(scanned) if scanned is not None else "—")
        total = c.guild_info.member_count if c.guild_info else None
        self.dashboard.card_scanned.set_caption(
            f"of {total:,} in server" + (" (bots ignored)" if c.settings.ignore_bots else "") if total else "Last completed scan"
        )

    def _on_scan_finished(self, result: ScanResult) -> None:
        panel = self.dashboard.activity.status
        panel.set_value("last", format_clock(result.completed_at))
        panel.set_value("duration", format_duration_ms(result.duration_ms))
        if result.status == "success":
            panel.set_value("checked", format_int(result.members_checked))
            panel.set_value("matches", f"{result.matches_found:,}" + (f"  (+{result.new_matches:,} new)" if result.new_matches else ""))
            if result.manual:
                self.toasts.show(
                    "success",
                    "Scan completed",
                    f"{result.members_checked:,} checked · {result.matches_found:,} matches · {result.new_matches:,} new",
                    3500,
                )
        else:
            panel.set_value("checked", "—")
            panel.set_value("matches", "Failed")
        if self.pages.currentIndex() == PAGES.index("activity"):
            self.activity_page.history_model.set_rows(self.controller.recent_scans())

    def _restore_last_scan(self) -> None:
        """Show the most recent persisted scan in the Scanner panel after a restart."""
        rows = self.controller.recent_scans()
        if not rows:
            return
        row = rows[0]
        panel = self.dashboard.activity.status
        panel.set_value("last", format_clock(from_iso(row["completed_at"])))
        panel.set_value("duration", format_duration_ms(row["duration_ms"]))
        if row["status"] == "success":
            panel.set_value("checked", format_int(row["members_checked"]))
            panel.set_value("matches", format_int(row["matches_found"]))

    def _on_connection(self, state_value: str, detail: str) -> None:
        state = ConnectionState(state_value)
        self.sidebar.connection.set_state(state, detail)
        self.dashboard.activity.status.set_value("connection", STATE_LABEL[state])
        self.dashboard.activity.status._values["connection"].setToolTip(detail)
        self._tick()

    def _on_guild(self, info: GuildInfo | None) -> None:
        if info is None:
            self.sidebar.connection.set_guild(None, None)
        else:
            self.sidebar.connection.set_guild(info.name, info.member_count)
        self._on_stats()

    def _on_scan_state(self, scanning: bool) -> None:
        self.dashboard.set_scanning(scanning)
        self._tick()

    def _on_paused(self, paused: bool) -> None:
        self.dashboard.set_paused(paused)
        self.toasts.show("info", "Monitoring paused" if paused else "Monitoring resumed", "", 2500)
        self._tick()

    def _on_settings_changed(self, settings) -> None:
        cutoff = settings.cutoff
        label = format_long_date(cutoff)
        if (cutoff.hour, cutoff.minute, cutoff.second) != (0, 0, 0):
            label += cutoff.strftime(" %H:%M UTC")
        short = cutoff.strftime("%b ") + str(cutoff.day) + cutoff.strftime(", %Y")
        self.dashboard.header.subtitle.setText(f"Automatically monitoring members who joined before {short}")
        self.members_page.header.subtitle.setText(f"Every stored member who joined before {label}")
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
            countdown, caption = "Scanning", "Scan in progress…"
        elif scanner.paused:
            countdown, caption = "Paused", "Monitoring paused"
        elif not c.discord.is_ready:
            countdown = "--:--"
            caption = "Waiting for Discord connection"
        else:
            remaining = scanner.seconds_until_next()
            countdown = format_countdown(remaining)
            caption = f"Next scan in {countdown} · every {c.settings.scan_interval}s"
        self.dashboard.card_next.set_value(countdown)
        self.dashboard.card_next.set_caption(caption)
        self.dashboard.activity.status.set_value("next", countdown)
        c.refresh_stats()  # throttled internally (keeps "Discovered Today" correct across midnight)

    # ----------------------------------------------------------------- export

    def _export(self, records: list[MemberRecord]) -> None:
        if not records:
            self.toasts.show("info", "Nothing to export", "No members match the current filters.")
            return
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
        suggested = str(Path(docs) / default_export_name(self.controller.settings.cutoff))
        path, _ = QFileDialog.getSaveFileName(self, "Export members to CSV", suggested, "CSV files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            count = export_csv(records, Path(path))
        except OSError as exc:
            self.toasts.show("error", "Export failed", str(exc))
            self.controller.log_activity("error", f"CSV export failed: {exc}")
            return
        self.toasts.show("success", "Export complete", f"{count:,} members written to {Path(path).name}")
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
