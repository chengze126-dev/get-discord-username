"""Dashboard page: header, statistic cards, member table and activity feed."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from app.avatars import AvatarCache
from app.database import ACTIVITY_LIMIT
from app.models import ActivityEvent
from app.utils import format_clock
from ui.components.icons import themed_icon
from ui.components.stat_card import StatCard
from ui.member_table import MemberTableModel, MemberTableSection
from ui.theme import FONT_FAMILIES, MONO_FAMILIES, Palette, ThemeManager

_EVENT_TOKEN = {
    "found": "success",
    "scan": "accent",
    "error": "danger",
    "warning": "warning",
    "connection": "warning",
    "info": "text_faint",
}


class ActivityModel(QAbstractListModel):
    EventRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._events: list[ActivityEvent] = []  # newest first

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._events)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        event = self._events[index.row()]
        if role == self.EventRole:
            return event
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{format_clock(event.timestamp)}  {event.message}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return event.message
        return None

    def set_events(self, events: list[ActivityEvent]) -> None:
        self.beginResetModel()
        self._events = list(events)[:ACTIVITY_LIMIT]
        self.endResetModel()

    def add_event(self, event: ActivityEvent) -> None:
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._events.insert(0, event)
        self.endInsertRows()
        if len(self._events) > ACTIVITY_LIMIT:
            start = ACTIVITY_LIMIT
            self.beginRemoveRows(QModelIndex(), start, len(self._events) - 1)
            del self._events[ACTIVITY_LIMIT:]
            self.endRemoveRows()


class ActivityDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, compact: bool = True) -> None:
        super().__init__(parent)
        self._compact = compact

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 30 if self._compact else 36)

    def paint(self, painter: QPainter, option, index) -> None:
        event: ActivityEvent = index.data(ActivityModel.EventRole)
        if event is None:
            return
        p = ThemeManager.instance().palette
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        mono = QFont()
        mono.setFamilies(MONO_FAMILIES)
        mono.setPixelSize(11)
        body = QFont()
        body.setFamilies(FONT_FAMILIES)
        body.setPixelSize(12 if self._compact else 13)

        color = QColor(getattr(p, _EVENT_TOKEN.get(event.event_type, "text_faint")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(rect.left() + 4, rect.center().y() - 3, 6, 6))

        painter.setFont(mono)
        painter.setPen(QColor(p.text_faint))
        time_rect = QRect(rect.left() + 18, rect.top(), 62, rect.height())
        painter.drawText(time_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), format_clock(event.timestamp))

        painter.setFont(body)
        painter.setPen(QColor(p.danger if event.event_type == "error" else p.text))
        text_rect = QRect(rect.left() + 84, rect.top(), rect.width() - 90, rect.height())
        text = QFontMetrics(body).elidedText(event.message, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


class ScanStatusPanel(QFrame):
    """Last Scan / Next Scan / Duration / Checked / Matches / Connection."""

    FIELDS = [
        ("last", "Last scan"),
        ("next", "Next scan"),
        ("duration", "Scan duration"),
        ("checked", "Members checked"),
        ("matches", "Matches found"),
        ("connection", "Connection"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DrawerSection")
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self._values: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(self.FIELDS):
            name = QLabel(label)
            name.setObjectName("Faint")
            value = QLabel("—")
            value.setObjectName("FieldValue")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
            self._values[key] = value

    def set_value(self, key: str, text: str) -> None:
        label = self._values[key]
        if label.text() != text:
            label.setText(text)


class ActivityFeedCard(QFrame):
    view_all = Signal()

    def __init__(self, model: ActivityModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFixedWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 14, 14)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("Scanner")
        title.setObjectName("H2")
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.status = ScanStatusPanel()
        layout.addWidget(self.status)

        feed_row = QHBoxLayout()
        feed_title = QLabel("Activity")
        feed_title.setObjectName("H2")
        feed_row.addWidget(feed_title)
        feed_row.addStretch(1)
        self._all = QPushButton("View all")
        self._all.setObjectName("GhostButton")
        self._all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._all.clicked.connect(self.view_all.emit)
        feed_row.addWidget(self._all)
        layout.addLayout(feed_row)

        self.feed = QListView()
        self.feed.setObjectName("ActivityFeed")
        self.feed.setModel(model)
        self.feed.setItemDelegate(ActivityDelegate(self.feed, compact=True))
        self.feed.setUniformItemSizes(True)
        self.feed.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.feed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.feed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.feed.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.feed, 1)
        ThemeManager.instance().changed.connect(lambda _p: self.feed.viewport().update())


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("H1")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Subtitle")
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        layout.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)


class DashboardPage(QWidget):
    pause_toggled = Signal()
    scan_now = Signal()

    def __init__(
        self,
        member_model: MemberTableModel,
        activity_model: ActivityModel,
        avatars: AvatarCache,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)

        self.header = PageHeader("Member Discovery", "Automatically monitoring members who joined before Jan 1, 2024")
        self.pause_button = QPushButton("Pause Monitoring")
        self.pause_button.setObjectName("SecondaryButton")
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.clicked.connect(self.pause_toggled.emit)
        self.scan_button = QPushButton("Scan Now")
        self.scan_button.setObjectName("PrimaryButton")
        self.scan_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_button.clicked.connect(self.scan_now.emit)
        self.header.actions.addWidget(self.pause_button)
        self.header.actions.addWidget(self.scan_button)
        layout.addWidget(self.header)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.card_qualifying = StatCard("Qualifying Members", "members", "accent")
        self.card_today = StatCard("Discovered Today", "sparkle", "success")
        self.card_scanned = StatCard("Members Scanned", "scan", "warning")
        self.card_next = StatCard("Next Scan", "clock", "accent")
        for card in (self.card_qualifying, self.card_today, self.card_scanned, self.card_next):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.members = MemberTableSection(member_model, avatars, "Members Found")
        self.activity = ActivityFeedCard(activity_model)
        body.addWidget(self.members, 1)
        body.addWidget(self.activity)
        layout.addLayout(body, 1)

        ThemeManager.instance().changed.connect(self._apply_theme)
        self._paused = False
        self._apply_theme(ThemeManager.instance().palette)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("Resume Monitoring" if paused else "Pause Monitoring")
        self._apply_theme(ThemeManager.instance().palette)

    def set_scanning(self, scanning: bool) -> None:
        self.scan_button.setEnabled(not scanning)
        self.scan_button.setText("Scanning…" if scanning else "Scan Now")

    def _apply_theme(self, p: Palette) -> None:
        self.pause_button.setIcon(themed_icon("play" if self._paused else "pause", p.text, 14))
        self.scan_button.setIcon(themed_icon("scan", p.accent_text, 15))
