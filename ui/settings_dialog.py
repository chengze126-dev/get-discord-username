"""Settings page (Discord, Monitoring, Notifications, Appearance, Security)."""

from __future__ import annotations

import html

from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QDate, QDateTime, Qt, QTime, QTimeZone, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import MAX_GUILDS, MAX_SCAN_INTERVAL, MIN_SCAN_INTERVAL, Settings, parse_guild_ids
from app.discord_service import BotGuild, ConnectionTestResult
from ui.components.icons import themed_icon
from ui.components.toggle_switch import ToggleSwitch
from ui.dashboard import PageHeader
from ui.theme import Palette, ThemeManager


@dataclass
class SettingsForm:
    settings: Settings
    new_token: str | None  # None = unchanged


def _qdatetime_from_utc(value: datetime) -> QDateTime:
    value = value.astimezone(timezone.utc)
    return QDateTime(
        QDate(value.year, value.month, value.day),
        QTime(value.hour, value.minute, value.second),
        QTimeZone.utc(),
    )


def _utc_from_qdatetime(value: QDateTime) -> datetime:
    utc = value.toUTC()
    d, t = utc.date(), utc.time()
    return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second(), tzinfo=timezone.utc)


class _Section(QFrame):
    def __init__(self, title: str, icon: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._icon_name = icon
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 18, 22, 8)
        self._layout.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(28, 28)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self._icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        label = QLabel(title)
        label.setObjectName("H2")
        text.addWidget(label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Faint")
            text.addWidget(sub)
        head.addLayout(text, 1)
        self._layout.addLayout(head)
        self._layout.addSpacing(12)
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    def _apply_theme(self, p: Palette) -> None:
        from ui.components.icons import icon_pixmap

        self._icon.setPixmap(icon_pixmap(self._icon_name, p.accent, 15))
        self._icon.setStyleSheet(f"background: {p.accent_soft}; border-radius: 8px;")

    def add_row(self, label: str, hint: str, control: QWidget | QHBoxLayout, stretch_control: bool = False) -> None:
        row = QFrame()
        row.setObjectName("SettingsRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(label)
        title.setObjectName("SettingLabel")
        text.addWidget(title)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("SettingHint")
            hint_label.setWordWrap(True)
            text.addWidget(hint_label)
        layout.addLayout(text, 1)
        if isinstance(control, QHBoxLayout):
            layout.addLayout(control, 1 if stretch_control else 0)
        else:
            layout.addWidget(control, 1 if stretch_control else 0, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(row)

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)


class GuildPickerDialog(QDialog):
    """Checklist of the servers the bot has been invited to."""

    def __init__(self, guilds: list[BotGuild], selected: tuple[int, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select servers")
        self.setMinimumWidth(460)
        p = ThemeManager.instance().palette
        self.setStyleSheet(f"QDialog {{ background: {p.surface}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        title = QLabel("Servers your bot is in")
        title.setObjectName("H2")
        hint = QLabel("Tick the servers to monitor. Members of each server are fetched and shown separately.")
        hint.setObjectName("SettingHint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        self._boxes: list[tuple[QCheckBox, int]] = []
        for guild in guilds:
            members = f"  ·  ~{guild.approximate_members:,} members" if guild.approximate_members else ""
            box = QCheckBox(f"{guild.name}   ({guild.id}){members}")
            box.setChecked(guild.id in selected)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(box)
            self._boxes.append((box, guild.id))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addSpacing(6)
        layout.addWidget(buttons)

    def selected_ids(self) -> list[int]:
        return [gid for box, gid in self._boxes if box.isChecked()]


class SettingsPage(QWidget):
    save_requested = Signal(object)          # SettingsForm
    test_requested = Signal(str, object)     # token ('' = stored), tuple of guild ids
    find_servers_requested = Signal(str)     # token ('' = stored)
    clear_token_requested = Signal()
    test_notification_requested = Signal()
    theme_preview = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._settings = Settings()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("ScrollContent")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)

        header = PageHeader("Settings", "Connection, monitoring rules, notifications and appearance")
        self.revert_button = QPushButton("Revert")
        self.revert_button.setObjectName("SecondaryButton")
        self.save_button = QPushButton("Save Changes")
        self.save_button.setObjectName("PrimaryButton")
        for b in (self.revert_button, self.save_button):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            header.actions.addWidget(b)
        self.revert_button.clicked.connect(lambda: self.load(self._settings, self._token_source, self._has_token))
        self.save_button.clicked.connect(self._on_save)
        layout.addWidget(header)

        column_host = QWidget()
        column_host.setMaximumWidth(1040)
        column = QVBoxLayout(column_host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(16)
        wrapper = QHBoxLayout()
        wrapper.addWidget(column_host, 1)
        wrapper.addStretch(0)
        layout.addLayout(wrapper)

        # ---------------------------------------------------------- Discord
        discord_section = _Section("Discord", "plug", "Official Bot API credentials and the servers the bot was invited to")
        token_row = QHBoxLayout()
        token_row.setSpacing(6)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setMinimumWidth(320)
        self.token.setPlaceholderText("Paste bot token")
        self.token.setToolTip("Stored in your OS credential store, never in the database or logs.")
        self._reveal = QPushButton()
        self._reveal.setObjectName("IconButton")
        self._reveal.setCheckable(True)
        self._reveal.setFixedSize(34, 34)
        self._reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reveal.setToolTip("Show / hide token")
        self._reveal.toggled.connect(self._toggle_reveal)
        self.clear_token = QPushButton("Remove")
        self.clear_token.setObjectName("DangerButton")
        self.clear_token.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_token.clicked.connect(self.clear_token_requested.emit)
        token_row.addWidget(self.token, 1)
        token_row.addWidget(self._reveal)
        token_row.addWidget(self.clear_token)
        discord_section.add_row("Bot Token", "", token_row, stretch_control=True)
        self.token_source = QLabel()
        self.token_source.setObjectName("SettingHint")
        self.token_source.setWordWrap(True)
        discord_section.add_widget(self.token_source)

        guild_row = QHBoxLayout()
        guild_row.setSpacing(6)
        self.guild = QPlainTextEdit()
        self.guild.setObjectName("GuildIds")
        self.guild.setPlaceholderText("123456789012345678\n987654321098765432")
        self.guild.setMinimumWidth(320)
        self.guild.setFixedHeight(86)
        self.guild.setTabChangesFocus(True)
        self.guild.textChanged.connect(self._update_guild_hint)
        self.find_servers = QPushButton("Find my servers…")
        self.find_servers.setObjectName("SecondaryButton")
        self.find_servers.setCursor(Qt.CursorShape.PointingHandCursor)
        self.find_servers.setToolTip("List the servers the bot has been invited to and pick them")
        self.find_servers.clicked.connect(self._on_find_servers)
        guild_row.addWidget(self.guild, 1)
        guild_row.addWidget(self.find_servers, 0, Qt.AlignmentFlag.AlignTop)
        discord_section.add_row(
            "Server (Guild) IDs",
            f"One or more IDs, one per line or separated by commas (up to {MAX_GUILDS}). Developer Mode → right-click a server "
            "icon → Copy Server ID. Saved to .env as DISCORD_GUILD_IDS.",
            guild_row,
            stretch_control=True,
        )
        self.guild_hint = QLabel()
        self.guild_hint.setObjectName("SettingHint")
        discord_section.add_widget(self.guild_hint)

        test_row = QHBoxLayout()
        test_row.setSpacing(10)
        self.test_button = QPushButton("Test Connection")
        self.test_button.setObjectName("SecondaryButton")
        self.test_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_button.clicked.connect(self._on_test)
        test_row.addWidget(self.test_button)
        test_row.addStretch(1)
        discord_section.add_row("Verify", "Checks the token, Server Members Intent and access to every server over REST.", test_row)
        self.test_result = QLabel()
        self.test_result.setObjectName("TestResult")
        self.test_result.setWordWrap(True)
        self.test_result.hide()
        discord_section.add_widget(self.test_result)
        spacer = QWidget()
        spacer.setFixedHeight(8)
        discord_section.add_widget(spacer)
        column.addWidget(discord_section)

        # ------------------------------------------------------- Monitoring
        monitor = _Section("Monitoring", "scan", "What qualifies a member and how often to check")
        self.cutoff = QDateTimeEdit()
        self.cutoff.setCalendarPopup(True)
        self.cutoff.setDisplayFormat("MMMM d, yyyy  HH:mm:ss 'UTC'")
        self.cutoff.setTimeZone(QTimeZone.utc())
        self.cutoff.setMinimumWidth(260)
        monitor.add_row("Cutoff Date", "Members whose server join date is strictly before this moment qualify.", self.cutoff)

        self.interval = QSpinBox()
        self.interval.setRange(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL)
        self.interval.setSingleStep(30)
        self.interval.setSuffix(" seconds")
        self.interval.setMinimumWidth(160)
        monitor.add_row("Scan Interval", f"Minimum {MIN_SCAN_INTERVAL} seconds.", self.interval)

        self.ignore_bots = ToggleSwitch(True)
        monitor.add_row("Ignore Bots", "Skip bot accounts when scanning.", self.ignore_bots)
        column.addWidget(monitor)

        # ---------------------------------------------------- Notifications
        notes = _Section("Notifications", "bell", "Native desktop alerts for newly discovered members")
        self.notifications = ToggleSwitch(True)
        notes.add_row("Desktop Notifications", "One notification per newly discovered member.", self.notifications)
        self.sound = ToggleSwitch(True)
        notes.add_row("Notification Sound", "Play a short chime with each notification.", self.sound)
        self.max_notes = QSpinBox()
        self.max_notes.setRange(1, 1000)
        self.max_notes.setSuffix(" per scan")
        self.max_notes.setMinimumWidth(160)
        notes.add_row(
            "Individual Notification Limit",
            "When a single scan discovers more members than this (e.g. the very first scan), the rest are "
            "summarised in one notification instead of flooding the desktop.",
            self.max_notes,
        )
        self.test_notification = QPushButton("Send Test Notification")
        self.test_notification.setObjectName("SecondaryButton")
        self.test_notification.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_notification.clicked.connect(self.test_notification_requested.emit)
        notes.add_row("Test", "", self.test_notification)
        column.addWidget(notes)

        # ------------------------------------------------------- Appearance
        appearance = _Section("Appearance", "sun", "Dark mode is the default")
        self.theme = QComboBox()
        for label, value in (("Dark", "dark"), ("Light", "light"), ("System", "system")):
            self.theme.addItem(label, value)
        self.theme.setMinimumWidth(160)
        self.theme.currentIndexChanged.connect(lambda _i: self.theme_preview.emit(self.theme.currentData()))
        appearance.add_row("Theme", "Applied immediately; saved with Save Changes.", self.theme)
        column.addWidget(appearance)

        # --------------------------------------------------------- Security
        security = _Section("Security", "shield", "How secrets and data are handled")
        info = QLabel(
            "• The bot token is kept in your operating system's credential store (Windows Credential Manager, "
            "macOS Keychain, Secret Service / KWallet) or read from the DISCORD_BOT_TOKEN environment variable. "
            "It is never written to SQLite or to log files.\n"
            "• Only the official Discord Bot API is used. The bot can only see servers it was explicitly invited to.\n"
            "• Member data is stored locally in data/scout.db and never leaves this computer."
        )
        info.setObjectName("SettingHint")
        info.setWordWrap(True)
        info.setContentsMargins(0, 4, 0, 14)
        security.add_widget(info)
        column.addWidget(security)
        layout.addStretch(1)

        self._token_source = "not configured"
        self._has_token = False
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    # ----------------------------------------------------------------- public

    def load(self, settings: Settings, token_source: str, has_token: bool) -> None:
        self._settings = settings
        self._token_source = token_source
        self._has_token = has_token
        self.token.clear()
        self.token.setPlaceholderText("•••••••••••••••••••••••• (saved — paste to replace)" if has_token else "Paste bot token")
        self.token_source.setText(f"Token source: {token_source}")
        removable = "credential store" in token_source or "session" in token_source
        self.clear_token.setEnabled(has_token and removable)
        self.guild.setPlainText("\n".join(str(g) for g in settings.guild_ids))
        self.cutoff.setDateTime(_qdatetime_from_utc(settings.cutoff))
        self.interval.setValue(settings.scan_interval)
        self.ignore_bots.setChecked(settings.ignore_bots)
        self.notifications.setChecked(settings.notifications_enabled)
        self.sound.setChecked(settings.notification_sound)
        self.max_notes.setValue(settings.max_notifications_per_scan)
        index = self.theme.findData(settings.theme)
        self.theme.blockSignals(True)
        self.theme.setCurrentIndex(max(0, index))
        self.theme.blockSignals(False)
        if ThemeManager.instance().mode != settings.theme:
            ThemeManager.instance().apply(settings.theme)  # undo an unsaved theme preview

    def current_guild_ids(self) -> tuple[int, ...]:
        return parse_guild_ids(self.guild.toPlainText())[:MAX_GUILDS]

    def show_bot_guilds(self, result) -> None:
        """Result of 'Find my servers…': list[BotGuild] or an error message."""
        self.find_servers.setEnabled(True)
        self.find_servers.setText("Find my servers…")
        if isinstance(result, str):
            self._show_message(False, "Could not list servers", [result])
            return
        if not result:
            self._show_message(
                False,
                "The bot is not in any server yet",
                ["Invite it with the OAuth2 URL Generator: scope = bot (not only applications.commands)."],
            )
            return
        dialog = GuildPickerDialog(result, self.current_guild_ids(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            known = {g.id for g in result}
            # keep manually entered IDs the bot can't see (so the user notices them), then the ticked ones
            kept = [g for g in self.current_guild_ids() if g not in known]
            self.guild.setPlainText("\n".join(str(g) for g in kept + dialog.selected_ids()))

    def show_test_result(self, result: ConnectionTestResult) -> None:
        self._show_message(result.ok, result.title, result.details)
        self.set_testing(False)

    def _show_message(self, ok: bool, title: str, details: list[str]) -> None:
        p = ThemeManager.instance().palette
        fg, bg = (p.success, p.success_soft) if ok else (p.danger, p.danger_soft)
        lines = "".join(f"<br>• {html.escape(line)}" for line in details)
        self.test_result.setText(f"<b style='color:{fg}'>{html.escape(title)}</b>{lines}")
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.test_result.setStyleSheet(f"background: {bg}; border: 1px solid {fg}; color: {p.text};")
        self.test_result.show()

    def set_testing(self, testing: bool) -> None:
        self.test_button.setEnabled(not testing)
        self.test_button.setText("Testing…" if testing else "Test Connection")

    # --------------------------------------------------------------- internal

    def _apply_theme(self, p: Palette) -> None:
        self._reveal.setIcon(themed_icon("eye-off" if self._reveal.isChecked() else "eye", p.text_muted, 16))
        self.test_button.setIcon(themed_icon("plug", p.text_muted, 15))
        self.test_notification.setIcon(themed_icon("bell", p.text_muted, 15))
        self.save_button.setIcon(themed_icon("check", p.accent_text, 15))

    def _toggle_reveal(self, shown: bool) -> None:
        self.token.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self._apply_theme(ThemeManager.instance().palette)

    def _on_test(self) -> None:
        self.set_testing(True)
        self.test_result.hide()
        self.test_requested.emit(self.token.text().strip(), self.current_guild_ids())

    def _on_find_servers(self) -> None:
        self.find_servers.setEnabled(False)
        self.find_servers.setText("Loading…")
        self.find_servers_requested.emit(self.token.text().strip())

    def _update_guild_hint(self) -> None:
        ids = self.current_guild_ids()
        raw = self.guild.toPlainText()
        raw_parts = [x for x in raw.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",") if x.strip()]
        invalid = len(raw_parts) - len(parse_guild_ids(raw))
        text = f"{len(ids)} server{'s' if len(ids) != 1 else ''} configured"
        if invalid > 0:
            text += f" · {invalid} value(s) ignored (IDs are 17–20 digits)"
        self.guild_hint.setText(text)

    def _on_save(self) -> None:
        settings = self._settings.with_changes(
            guild_ids=self.current_guild_ids(),
            cutoff=_utc_from_qdatetime(self.cutoff.dateTime()),
            scan_interval=self.interval.value(),
            ignore_bots=self.ignore_bots.isChecked(),
            notifications_enabled=self.notifications.isChecked(),
            notification_sound=self.sound.isChecked(),
            max_notifications_per_scan=self.max_notes.value(),
            theme=self.theme.currentData(),
        )
        token = self.token.text().strip() or None
        self.save_requested.emit(SettingsForm(settings=settings, new_token=token))
        self.token.clear()
        self._reveal.setChecked(False)
