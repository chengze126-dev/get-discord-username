"""Horizontal strip of server cards: 'All Servers' plus one card per Guild ID.

Each card shows the server name, Guild ID, Discord's member count, how many
members are loaded, a status pill with a progress bar while loading, the last
sync time and a refresh button. Clicking a card selects that server.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.avatars import AvatarCache
from app.models import GUILD_STATUS_LABEL, GuildState, GuildStatus
from app.utils import format_clock
from ui.components.avatar import fallback_color, initials
from ui.components.icons import themed_icon
from ui.theme import Palette, ThemeManager

CARD_WIDTH = 256
CARD_HEIGHT = 128

_STATUS_TOKEN = {
    GuildStatus.PENDING: "text_faint",
    GuildStatus.AVAILABLE: "accent",
    GuildStatus.LOADING: "warning",
    GuildStatus.SYNCED: "success",
    GuildStatus.ERROR: "danger",
    GuildStatus.NOT_MEMBER: "danger",
    GuildStatus.UNAVAILABLE: "warning",
}


class _ServerIcon(QWidget):
    def __init__(self, avatars: AvatarCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._avatars = avatars
        self._url: str | None = None
        self._guild_id = 0
        self._name = ""
        self._all = False
        self.setFixedSize(34, 34)
        avatars.avatar_ready.connect(lambda url: self.update() if url == self._url else None)

    def set_server(self, url: str | None, guild_id: int, name: str, is_all: bool = False) -> None:
        self._url, self._guild_id, self._name, self._all = url, guild_id, name, is_all
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = ThemeManager.instance().palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, 33, 33)
        pixmap = self._avatars.rounded(self._url, 33, self.devicePixelRatioF()) if self._url else None
        if pixmap is not None:
            painter.drawPixmap(rect.toRect(), pixmap)
        else:
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            painter.fillPath(path, QColor(p.accent) if self._all else fallback_color(self._guild_id))
            font = QFont(self.font())
            font.setPixelSize(14)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "∗" if self._all else initials(self._name))
        painter.end()


class ServerCard(QFrame):
    clicked = Signal(object)            # guild id | None
    refresh_clicked = Signal(object)

    def __init__(self, avatars: AvatarCache, guild_id: int | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.guild_id = guild_id
        self.setObjectName("ServerCard")
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected = False
        self._state: GuildState | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 12, 10)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon = _ServerIcon(avatars)
        top.addWidget(self.icon)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name = QLabel()
        self.name.setObjectName("CardTitle")
        self.gid = QLabel()
        self.gid.setObjectName("CardMono")
        names.addWidget(self.name)
        names.addWidget(self.gid)
        top.addLayout(names, 1)
        self.refresh = QPushButton()
        self.refresh.setObjectName("IconButton")
        self.refresh.setFixedSize(28, 28)
        self.refresh.setToolTip("Refresh this server")
        self.refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh.clicked.connect(lambda: self.refresh_clicked.emit(self.guild_id) if self.guild_id else None)
        self.refresh.setVisible(guild_id is not None)
        top.addWidget(self.refresh, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        self.stats = QLabel()
        self.stats.setObjectName("CardStats")
        layout.addWidget(self.stats)

        self.progress = QProgressBar()
        self.progress.setObjectName("CardProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.status = QLabel()
        self.status.setObjectName("StatusPill")
        self.synced = QLabel()
        self.synced.setObjectName("Faint")
        bottom.addWidget(self.status)
        bottom.addStretch(1)
        bottom.addWidget(self.synced)
        layout.addLayout(bottom)

        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    # --- public
    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_theme(ThemeManager.instance().palette)

    def set_all(self, states: list[GuildState]) -> None:
        total = sum(s.member_count or 0 for s in states)
        loaded = sum(s.loaded for s in states)
        loading = [s for s in states if s.status == GuildStatus.LOADING]
        problems = [s for s in states if s.status in (GuildStatus.ERROR, GuildStatus.NOT_MEMBER, GuildStatus.UNAVAILABLE)]
        self.icon.set_server(None, 0, "All", is_all=True)
        self.name.setText("All Servers")
        self.gid.setText(f"{len(states)} server{'s' if len(states) != 1 else ''} configured")
        self.stats.setText(f"<b>{total:,}</b> members  ·  <b>{loaded:,}</b> loaded")
        if loading:
            self._set_status(GuildStatus.LOADING, f"Loading {len(loading)}")
            done = sum((s.progress or 0) for s in loading) / len(loading)
            self.progress.setValue(int(done * 1000))
            self.progress.show()
        else:
            self.progress.hide()
            if problems:
                self._set_status(GuildStatus.ERROR, f"{len(problems)} need attention")
            elif states:
                self._set_status(GuildStatus.SYNCED, "All synced" if all(s.last_synced_at for s in states) else "Ready")
            else:
                self._set_status(GuildStatus.PENDING, "No servers")
        synced = [s.last_synced_at for s in states if s.last_synced_at]
        self.synced.setText(f"Synced {format_clock(max(synced))}" if synced else "")
        self.setToolTip("Show members of every configured server, grouped by server")

    def set_state(self, state: GuildState) -> None:
        self._state = state
        self.icon.set_server(state.icon_url, state.guild_id, state.display_name)
        metrics = self.name.fontMetrics()
        self.name.setText(metrics.elidedText(state.display_name, Qt.TextElideMode.ElideRight, CARD_WIDTH - 110))
        self.gid.setText(str(state.guild_id))
        total = f"{state.member_count:,}" if state.member_count is not None else "—"
        self.stats.setText(f"<b>{total}</b> members  ·  <b>{state.loaded:,}</b> loaded")
        self._set_status(state.status, GUILD_STATUS_LABEL.get(state.status, ""))
        if state.status == GuildStatus.LOADING:
            self.progress.setRange(0, 1000 if state.progress is not None else 0)  # 0..0 = busy indicator
            self.progress.setValue(int((state.progress or 0) * 1000))
            self.progress.show()
        else:
            self.progress.hide()
        self.synced.setText(f"Synced {format_clock(state.last_synced_at)}" if state.last_synced_at else "Never synced")
        tip = [state.display_name, f"Guild ID {state.guild_id}"]
        if state.message:
            tip.append(state.message)
        self.setToolTip("\n".join(tip))
        self.refresh.setEnabled(state.status not in (GuildStatus.LOADING, GuildStatus.NOT_MEMBER))

    # --- internal
    def _set_status(self, status: GuildStatus, text: str) -> None:
        p = ThemeManager.instance().palette
        color = QColor(getattr(p, _STATUS_TOKEN.get(status, "text_faint")))
        self.status.setText(f"●  {text}")
        self.status.setStyleSheet(
            f"color: {color.name()}; background: rgba({color.red()},{color.green()},{color.blue()},0.14);"
            "border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600;"
        )

    def _apply_theme(self, p: Palette) -> None:
        border = p.accent if self._selected else p.border
        background = p.accent_soft if self._selected else p.surface
        self.setStyleSheet(
            f"QFrame#ServerCard {{ background: {background}; border: 1px solid {border}; border-radius: 14px; }}"
            f"QFrame#ServerCard:hover {{ border-color: {p.accent if self._selected else p.border_strong}; }}"
            f"QLabel#CardTitle {{ font-weight: 700; font-size: 13px; color: {p.text}; background: transparent; }}"
            f"QLabel#CardMono {{ font-family: 'JetBrains Mono','Cascadia Mono','Consolas','DejaVu Sans Mono',monospace;"
            f" font-size: 11px; color: {p.text_faint}; background: transparent; }}"
            f"QLabel#CardStats {{ font-size: 12px; color: {p.text_muted}; background: transparent; }}"
            f"QProgressBar#CardProgress {{ background: {p.border}; border: none; border-radius: 2px; }}"
            f"QProgressBar#CardProgress::chunk {{ background: {p.warning}; border-radius: 2px; }}"
        )
        self.refresh.setIcon(themed_icon("refresh", p.text_muted, 14))
        if self._state is not None:
            self._set_status(self._state.status, GUILD_STATUS_LABEL.get(self._state.status, ""))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.guild_id)
        super().mousePressEvent(event)


class ServerStrip(QScrollArea):
    server_selected = Signal(object)     # guild id | None
    refresh_requested = Signal(object)

    def __init__(self, avatars: AvatarCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._avatars = avatars
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(CARD_HEIGHT + 14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        content = QWidget()
        content.setObjectName("ScrollContent")
        content.setStyleSheet("QWidget#ScrollContent { background: transparent; }")
        self._row = QHBoxLayout(content)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(12)
        self._row.addStretch(1)
        self.setWidget(content)
        self.viewport().setStyleSheet("background: transparent;")
        self._all_card = self._make_card(None)
        self._row.insertWidget(0, self._all_card)
        self._cards: dict[int, ServerCard] = {}
        self._selected: int | None = None
        self._states: list[GuildState] = []
        self._all_card.set_selected(True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(CARD_WIDTH * 3, CARD_HEIGHT + 14)

    def _make_card(self, guild_id: int | None) -> ServerCard:
        card = ServerCard(self._avatars, guild_id)
        card.clicked.connect(self.select)
        card.refresh_clicked.connect(self.refresh_requested.emit)
        return card

    def set_guilds(self, states: list[GuildState]) -> None:
        self._states = list(states)
        wanted = [s.guild_id for s in states]
        for gid in list(self._cards):
            if gid not in wanted:
                card = self._cards.pop(gid)
                self._row.removeWidget(card)
                card.deleteLater()
        for position, state in enumerate(states, start=1):
            card = self._cards.get(state.guild_id)
            if card is None:
                card = self._make_card(state.guild_id)
                self._cards[state.guild_id] = card
            self._row.insertWidget(position, card)
            card.set_state(state)
        self._all_card.set_all(self._states)
        if self._selected is not None and self._selected not in self._cards:
            self.select(None)
        else:
            self._update_selection()

    def update_guild(self, state: GuildState) -> None:
        card = self._cards.get(state.guild_id)
        if card is not None:
            card.set_state(state)
        self._all_card.set_all(self._states)

    def select(self, guild_id: int | None, emit: bool = True) -> None:
        changed = guild_id != self._selected
        self._selected = guild_id
        self._update_selection()
        if emit and changed:
            self.server_selected.emit(guild_id)

    def _update_selection(self) -> None:
        self._all_card.set_selected(self._selected is None)
        for gid, card in self._cards.items():
            card.set_selected(gid == self._selected)
