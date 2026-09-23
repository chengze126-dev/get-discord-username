"""Slide-in member details drawer."""

from __future__ import annotations

import html

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.avatars import AvatarCache
from app.models import MemberRecord
from app.utils import format_long_date, format_precise
from ui.components.avatar import AvatarWidget
from ui.components.icons import themed_icon
from ui.theme import Palette, ThemeManager

DRAWER_WIDTH = 440


class _Scrim(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._alpha = 0.0

    def set_alpha(self, alpha: float) -> None:
        self._alpha = alpha
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = ThemeManager.instance().palette
        color = QColor(p.scrim)
        color.setAlphaF(self._alpha)
        painter = QPainter(self)
        painter.fillRect(self.rect(), color)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        event.accept()


class _Field(QWidget):
    def __init__(self, label: str, mono: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title = QLabel(label.upper())
        title.setObjectName("FieldLabel")
        self.value = QLabel("—")
        self.value.setObjectName("Mono" if mono else "FieldValue")
        self.value.setWordWrap(True)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(title)
        layout.addWidget(self.value)


class MemberDetailsDrawer(QFrame):
    copied = Signal(str)  # message for a toast

    def __init__(self, host: QWidget, avatars: AvatarCache) -> None:
        super().__init__(host)
        self.setObjectName("Drawer")
        self._host = host
        self._record: MemberRecord | None = None
        self._open = False

        self._scrim = _Scrim(host)
        self._scrim.clicked.connect(self.close_drawer)
        self._scrim.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        top = QHBoxLayout()
        caption = QLabel("MEMBER DETAILS")
        caption.setObjectName("FieldLabel")
        top.addWidget(caption)
        top.addStretch(1)
        self._close = QPushButton()
        self._close.setObjectName("IconButton")
        self._close.setFixedSize(30, 30)
        self._close.setIconSize(QSize(16, 16))
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.setToolTip("Close (Esc)")
        self._close.clicked.connect(self.close_drawer)
        top.addWidget(self._close)
        layout.addLayout(top)

        identity = QHBoxLayout()
        identity.setSpacing(16)
        self._avatar = AvatarWidget(avatars, 68)
        identity.addWidget(self._avatar)
        names = QVBoxLayout()
        names.setSpacing(2)
        self._username = QLabel()
        self._username.setObjectName("H1")
        self._username.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._display = QLabel()
        self._display.setObjectName("Subtitle")
        self._status = QLabel()
        self._status.setObjectName("Pill")
        names.addStretch(1)
        names.addWidget(self._username)
        names.addWidget(self._display)
        status_row = QHBoxLayout()
        status_row.addWidget(self._status)
        status_row.addStretch(1)
        names.addSpacing(4)
        names.addLayout(status_row)
        names.addStretch(1)
        identity.addLayout(names, 1)
        layout.addLayout(identity)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._copy_name = QPushButton("Copy Username")
        self._copy_name.setObjectName("SecondaryButton")
        self._copy_id = QPushButton("Copy Discord ID")
        self._copy_id.setObjectName("SecondaryButton")
        for button in (self._copy_name, self._copy_id):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        self._copy_name.clicked.connect(self._on_copy_name)
        self._copy_id.clicked.connect(self._on_copy_id)
        layout.addLayout(actions)

        info = QFrame()
        info.setObjectName("DrawerSection")
        grid = QGridLayout(info)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(14)
        self._f_server = _Field("Server")
        self._f_guild = _Field("Guild ID", mono=True)
        self._f_display = _Field("Display name")
        self._f_id = _Field("Discord user ID", mono=True)
        self._f_bot = _Field("Account type")
        self._f_count = _Field("Accessible channels")
        self._f_joined = _Field("Server joined date")
        self._f_first = _Field("First detected")
        self._f_last = _Field("Last seen")
        self._f_roles = _Field("Roles")
        self._f_roles.value.setTextFormat(Qt.TextFormat.RichText)
        grid.addWidget(self._f_server, 0, 0)
        grid.addWidget(self._f_guild, 0, 1)
        grid.addWidget(self._f_display, 1, 0)
        grid.addWidget(self._f_id, 1, 1)
        grid.addWidget(self._f_bot, 2, 0)
        grid.addWidget(self._f_count, 2, 1)
        grid.addWidget(self._f_joined, 3, 0, 1, 2)
        grid.addWidget(self._f_first, 4, 0, 1, 2)
        grid.addWidget(self._f_last, 5, 0, 1, 2)
        grid.addWidget(self._f_roles, 6, 0, 1, 2)
        layout.addWidget(info)

        channels_header = QHBoxLayout()
        title = QLabel("Channels")
        title.setObjectName("H2")
        self._channel_badge = QLabel("0")
        self._channel_badge.setObjectName("CountBadge")
        channels_header.addWidget(title)
        channels_header.addWidget(self._channel_badge)
        channels_header.addStretch(1)
        layout.addLayout(channels_header)

        note = QLabel(
            "Channels this member can currently view, computed from roles and permission overwrites. "
            "Discord does not record when a member first accessed a channel."
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._channels = QListWidget()
        self._channels.setObjectName("ChannelList")
        self._channels.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._channels.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._channels.setIconSize(QSize(15, 15))
        self._channels.setUniformItemSizes(True)
        self._channels.setSpacing(1)
        layout.addWidget(self._channels, 1)

        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scrim_timer = QTimer(self)
        self._scrim_timer.setInterval(16)
        self._scrim_timer.timeout.connect(self._sync_scrim)
        self._anim.finished.connect(self._on_anim_finished)

        host.installEventFilter(self)
        self.hide()
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    # ----------------------------------------------------------------- public

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def current_key(self) -> tuple[int, int] | None:
        """(guild_id, user_id) of the member shown - the same user can be in several servers."""
        return (self._record.guild_id, self._record.user_id) if self._record else None

    def show_member(self, record: MemberRecord) -> None:
        self._populate(record)
        if not self._open:
            self._open = True
            self._scrim.setGeometry(self._host.rect())
            self._scrim.show()
            self._scrim.raise_()
            self.setFixedSize(DRAWER_WIDTH, self._host.height())
            self.move(self._host.width(), 0)
            self.show()
            self.raise_()
            self._anim.stop()
            self._anim.setStartValue(QPoint(self._host.width(), 0))
            self._anim.setEndValue(QPoint(self._host.width() - DRAWER_WIDTH, 0))
            self._anim.start()
            self._scrim_timer.start()
        self.setFocus()

    def refresh(self, record: MemberRecord) -> None:
        if self._record is not None and (record.guild_id, record.user_id) == self.current_key:
            self._populate(record)

    def close_drawer(self) -> None:
        if not self._open:
            return
        self._open = False
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(self._host.width(), 0))
        self._anim.start()
        self._scrim_timer.start()

    # --------------------------------------------------------------- internal

    def _populate(self, record: MemberRecord) -> None:
        self._record = record
        p = ThemeManager.instance().palette
        self._avatar.set_member(record.avatar_url, record.user_id, record.username)
        self._username.setText(record.username)
        self._display.setText(record.display_name if record.shows_display_name else "")
        self._display.setVisible(record.shows_display_name)
        if record.in_guild:
            self._status.setText(f"Member since {format_long_date(record.joined_at)}")
        else:
            self._status.setText("No longer in this server")
        self._f_server.value.setText(record.guild_name or f"Server {record.guild_id}")
        self._f_guild.value.setText(str(record.guild_id))
        self._f_bot.value.setText("Bot account" if record.is_bot else "User account")
        if record.roles:
            chips = []
            for role in record.roles:
                color = role.hex_color or p.text_faint
                chips.append(f"<span style='color:{color}'>●</span>&nbsp;{html.escape(role.name)}")
            self._f_roles.value.setText("&nbsp;&nbsp; ".join(chips))
        else:
            self._f_roles.value.setText("No roles")
        self._f_display.value.setText(record.display_name or record.username)
        self._f_id.value.setText(str(record.user_id))
        self._f_joined.value.setText(format_precise(record.joined_at))
        self._f_first.value.setText(format_precise(record.first_detected_at))
        self._f_last.value.setText(format_precise(record.last_seen_at))
        self._f_count.value.setText(f"{record.channel_count} channel{'s' if record.channel_count != 1 else ''}")
        self._channel_badge.setText(str(record.channel_count))

        self._channels.clear()
        if not record.channels:
            item = QListWidgetItem("No viewable channels")
            item.setForeground(QColor(p.text_faint))
            self._channels.addItem(item)
        for channel in record.channels:
            icon = "voice" if channel.kind in ("voice", "stage") else "hash"
            label = channel.name if channel.kind in ("voice", "stage") else f"{channel.name}"
            item = QListWidgetItem(themed_icon(icon, p.text_muted, 15), label)
            suffix = {"voice": "Voice", "stage": "Stage", "forum": "Forum", "news": "Announcement", "media": "Media"}.get(channel.kind)
            item.setToolTip(f"{channel.label}  ·  {suffix or 'Text'} channel  ·  ID {channel.id}")
            item.setSizeHint(QSize(0, 30))
            self._channels.addItem(item)

    def _apply_theme(self, p: Palette) -> None:
        self._close.setIcon(themed_icon("close", p.text_muted, 16))
        self._copy_name.setIcon(themed_icon("copy", p.text_muted, 15))
        self._copy_id.setIcon(themed_icon("copy", p.text_muted, 15))
        if self._record is not None:
            self._populate(self._record)
        self._scrim.update()

    def _on_copy_name(self) -> None:
        if self._record:
            QGuiApplication.clipboard().setText(self._record.username)
            self.copied.emit(f"Copied username “{self._record.username}”")

    def _on_copy_id(self) -> None:
        if self._record:
            QGuiApplication.clipboard().setText(str(self._record.user_id))
            self.copied.emit(f"Copied Discord ID {self._record.user_id}")

    def _sync_scrim(self) -> None:
        width = self._host.width()
        progress = (width - self.x()) / DRAWER_WIDTH if DRAWER_WIDTH else 1.0
        self._scrim.set_alpha(max(0.0, min(1.0, progress)) * (0.45 if ThemeManager.instance().palette.is_dark else 0.25))

    def _on_anim_finished(self) -> None:
        self._scrim_timer.stop()
        self._sync_scrim()
        if not self._open:
            self.hide()
            self._scrim.hide()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_drawer()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._host and event.type() == QEvent.Type.Resize:
            self._scrim.setGeometry(self._host.rect())
            if self.isVisible():
                self.setFixedSize(DRAWER_WIDTH, self._host.height())
                x = self._host.width() - DRAWER_WIDTH if self._open else self._host.width()
                self._anim.stop()
                self.move(x, 0)
        return False
