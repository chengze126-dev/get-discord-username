"""Member table: Qt model/proxy, custom delegates, toolbar and CSV export."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.avatars import AvatarCache
from app.models import MemberRecord
from app.utils import format_date, format_precise, to_iso
from ui.components.avatar import paint_avatar
from ui.components.icons import icon_pixmap, themed_icon
from ui.theme import Palette, ThemeManager

COL_NO, COL_USER, COL_JOINED, COL_CHANNELS = range(4)
HEADERS = ["NO.", "USERNAME", "JOINED DATE", "JOINED CHANNELS"]
RecordRole = Qt.ItemDataRole.UserRole + 1
ROW_HEIGHT = 56

FILTERS = [
    ("All", None),
    ("Before 2020", "before2020"),
    ("2020", 2020),
    ("2021", 2021),
    ("2022", 2022),
    ("2023", 2023),
]
SORTS = [
    ("Oldest joined", "oldest"),
    ("Newest joined", "newest"),
    ("Username A-Z", "az"),
    ("Username Z-A", "za"),
    ("Recently discovered", "recent"),
]


# ============================================================================ model


class MemberTableModel(QAbstractTableModel):
    """Holds member records; supports incremental updates keyed by user ID."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._records: list[MemberRecord] = []
        self._index: dict[int, int] = {}
        self._avatar_rows: dict[str, list[int]] = {}

    # --- Qt API
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return HEADERS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._records):
            return None
        record = self._records[index.row()]
        column = index.column()
        if role == RecordRole:
            return record
        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_USER:
                return record.username
            if column == COL_JOINED:
                return format_date(record.joined_at)
            if column == COL_CHANNELS:
                return f"{record.channel_count} channels"
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_JOINED:
                return format_precise(record.joined_at)
            if column == COL_CHANNELS:
                return "Channels this member can view (based on current permissions). Click for details."
            if column == COL_USER and record.shows_display_name:
                return f"{record.username} · {record.display_name}"
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # --- updates
    def set_records(self, records: list[MemberRecord]) -> None:
        self.beginResetModel()
        self._records = list(records)
        self._rebuild_index()
        self.endResetModel()

    def upsert_records(self, records: list[MemberRecord]) -> None:
        """Update changed rows in place and append new ones without a model reset."""
        new: list[MemberRecord] = []
        for record in records:
            row = self._index.get(record.user_id)
            if row is None:
                new.append(record)
                continue
            if self._records[row] != record:
                self._records[row] = record
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))
        if new:
            start = len(self._records)
            self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
            self._records.extend(new)
            self.endInsertRows()
        self._rebuild_index()

    def sync(self, records: list[MemberRecord]) -> None:
        """Make the model match ``records`` with minimal churn."""
        wanted = {r.user_id for r in records}
        if any(r.user_id not in wanted for r in self._records):
            self.set_records(records)  # removals are rare (cutoff/guild changed)
        else:
            self.upsert_records(records)

    def record_at(self, row: int) -> MemberRecord | None:
        return self._records[row] if 0 <= row < len(self._records) else None

    def refresh_avatar(self, url: str) -> None:
        for row in self._avatar_rows.get(url, []):
            idx = self.index(row, COL_USER)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def _rebuild_index(self) -> None:
        self._index = {r.user_id: i for i, r in enumerate(self._records)}
        self._avatar_rows = {}
        for i, r in enumerate(self._records):
            if r.avatar_url:
                self._avatar_rows.setdefault(r.avatar_url, []).append(i)


class MemberFilterProxy(QSortFilterProxyModel):
    """Search / year filter / sort mode, with 'No.' as the displayed row number."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._search = ""
        self._year_filter: int | str | None = None
        self._sort_mode = "oldest"
        self.setDynamicSortFilter(True)

    def set_search(self, text: str) -> None:
        self._search = text.strip().casefold()
        self.invalidateFilter()

    def set_year_filter(self, value) -> None:
        self._year_filter = value
        self.invalidateFilter()

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode
        self.invalidate()
        self.sort(0, Qt.SortOrder.AscendingOrder)

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        record: MemberRecord | None = self.sourceModel().record_at(source_row)  # type: ignore[union-attr]
        if record is None:
            return False
        if self._search:
            haystack = f"{record.username}\n{record.display_name}\n{record.user_id}".casefold()
            if self._search not in haystack:
                return False
        if self._year_filter is not None:
            if record.joined_at is None:
                return False
            year = record.joined_at.year  # stored as UTC, like the cutoff
            if self._year_filter == "before2020":
                if year >= 2020:
                    return False
            elif year != self._year_filter:
                return False
        return True

    def lessThan(self, left, right) -> bool:  # noqa: N802
        model = self.sourceModel()
        a: MemberRecord = model.record_at(left.row())  # type: ignore[union-attr]
        b: MemberRecord = model.record_at(right.row())  # type: ignore[union-attr]
        mode = self._sort_mode
        far = datetime.max.replace(tzinfo=None)
        if mode in ("oldest", "newest"):
            ka = (a.joined_at.replace(tzinfo=None) if a.joined_at else far, a.user_id)
            kb = (b.joined_at.replace(tzinfo=None) if b.joined_at else far, b.user_id)
            return ka < kb if mode == "oldest" else ka > kb
        if mode in ("az", "za"):
            ka = (a.username.casefold(), a.user_id)
            kb = (b.username.casefold(), b.user_id)
            return ka < kb if mode == "az" else ka > kb
        # recently discovered: newest first_detected_at first
        ka = (a.first_detected_at.replace(tzinfo=None) if a.first_detected_at else datetime.min, a.user_id)
        kb = (b.first_detected_at.replace(tzinfo=None) if b.first_detected_at else datetime.min, b.user_id)
        return ka > kb

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and index.column() == COL_NO and role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        return super().data(index, role)

    def visible_records(self) -> list[MemberRecord]:
        return [self.data(self.index(row, 0), RecordRole) for row in range(self.rowCount())]


# ======================================================================== delegates


class _BaseDelegate(QStyledItemDelegate):
    """Paints row background (hover/selection) and the bottom separator."""

    def __init__(self, view: "MemberTableView") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(super().sizeHint(option, index).width(), ROW_HEIGHT)

    def paint_background(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        p = ThemeManager.instance().palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == self._view.hover_row
        painter.save()
        if selected:
            painter.fillRect(rect, QColor(p.selection))
        elif hovered:
            painter.fillRect(rect, QColor(p.hover))
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()

    def record(self, index) -> MemberRecord | None:
        return index.data(RecordRole)


class TextDelegate(_BaseDelegate):
    def __init__(self, view, muted: bool = False, mono: bool = False) -> None:
        super().__init__(view)
        self._muted = muted
        self._mono = mono

    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        p = ThemeManager.instance().palette
        record = self.record(index)
        painter.save()
        color = QColor(p.text_faint if self._muted else p.text)
        if record is not None and not record.in_guild:
            color.setAlphaF(0.55)
        painter.setPen(color)
        font = QFont(option.font)
        if self._mono:
            font.setPixelSize(12)
        painter.setFont(font)
        rect = option.rect.adjusted(16, 0, -12, 0)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


class UserDelegate(_BaseDelegate):
    AVATAR = 32

    def __init__(self, view, avatars: AvatarCache) -> None:
        super().__init__(view)
        self._avatars = avatars

    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not record.in_guild:
            painter.setOpacity(0.55)
        avatar_rect = QRectF(rect.left() + 14, rect.center().y() - self.AVATAR / 2 + 1, self.AVATAR, self.AVATAR)
        paint_avatar(
            painter, avatar_rect, self._avatars, record.avatar_url, record.user_id, record.username,
            self._view.devicePixelRatioF(),
        )

        text_left = int(avatar_rect.right()) + 12
        text_width = rect.right() - text_left - 12
        name_font = QFont(option.font)
        name_font.setWeight(QFont.Weight.DemiBold)
        sub_font = QFont(option.font)
        sub_font.setPixelSize(12)
        name_metrics = QFontMetrics(name_font)
        sub_metrics = QFontMetrics(sub_font)

        if record.shows_display_name:
            total = name_metrics.height() + sub_metrics.height()
            top = rect.center().y() - total // 2
            name_rect = QRect(text_left, top, text_width, name_metrics.height())
            sub_rect = QRect(text_left, top + name_metrics.height(), text_width, sub_metrics.height())
        else:
            name_rect = QRect(text_left, rect.top(), text_width, rect.height())
            sub_rect = None

        painter.setFont(name_font)
        painter.setPen(QColor(p.text))
        name = name_metrics.elidedText(record.username, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(name_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), name)
        if sub_rect is not None:
            painter.setFont(sub_font)
            painter.setPen(QColor(p.text_muted))
            sub = sub_metrics.elidedText(record.display_name, Qt.TextElideMode.ElideRight, text_width)
            painter.drawText(sub_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), sub)

        if not record.in_guild:
            painter.setOpacity(1.0)
            tag_font = QFont(option.font)
            tag_font.setPixelSize(10)
            tag_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(tag_font)
            label = "NOT IN SERVER"
            fm = QFontMetrics(tag_font)
            used = name_metrics.horizontalAdvance(name)
            tag_rect = QRectF(name_rect.left() + used + 8, name_rect.center().y() - 8, fm.horizontalAdvance(label) + 12, 16)
            if tag_rect.right() < rect.right() - 8:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(p.warning_soft))
                painter.drawRoundedRect(tag_rect, 8, 8)
                painter.setPen(QColor(p.warning))
                painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()


class ChannelsDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        hovered = index.row() == self._view.hover_row and self._view.hover_column == COL_CHANNELS
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(option.font)
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        text = f"{record.channel_count} channel{'s' if record.channel_count != 1 else ''}"
        fm = QFontMetrics(font)
        width = fm.horizontalAdvance(text) + 40
        rect = QRectF(option.rect.left() + 14, option.rect.center().y() - 13, width, 26)
        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)
        painter.fillPath(path, QColor(p.accent_soft if hovered else p.surface_alt))
        painter.setPen(QPen(QColor(p.accent if hovered else p.border), 1))
        painter.drawPath(path)
        icon = icon_pixmap("hash", p.accent if hovered else p.text_muted, 13)
        painter.drawPixmap(int(rect.left() + 10), int(rect.center().y() - 6.5), icon)
        painter.setPen(QColor(p.accent if hovered else p.text))
        painter.drawText(rect.adjusted(28, 0, -10, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


# ============================================================================= view


class MemberTableView(QTableView):
    member_activated = Signal(object)  # MemberRecord

    def __init__(self, avatars: AvatarCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MemberTable")
        self.hover_row = -1
        self.hover_column = -1
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setCornerButtonEnabled(False)

        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumHeight(38)

        self.setItemDelegateForColumn(COL_NO, TextDelegate(self, muted=True, mono=True))
        self.setItemDelegateForColumn(COL_USER, UserDelegate(self, avatars))
        self.setItemDelegateForColumn(COL_JOINED, TextDelegate(self))
        self.setItemDelegateForColumn(COL_CHANNELS, ChannelsDelegate(self))

        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)
        ThemeManager.instance().changed.connect(lambda _p: self.viewport().update())

    def configure_columns(self) -> None:
        header = self.horizontalHeader()
        header.setSectionResizeMode(COL_NO, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_USER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_JOINED, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_CHANNELS, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COL_NO, 72)
        header.resizeSection(COL_JOINED, 170)
        header.resizeSection(COL_CHANNELS, 190)

    def _record(self, index) -> MemberRecord | None:
        return index.data(RecordRole) if index.isValid() else None

    def _on_clicked(self, index) -> None:
        if index.column() == COL_CHANNELS:
            record = self._record(index)
            if record is not None:
                self.member_activated.emit(record)

    def _on_double_clicked(self, index) -> None:
        record = self._record(index)
        if record is not None:
            self.member_activated.emit(record)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            record = self._record(self.currentIndex())
            if record is not None:
                self.member_activated.emit(record)
                return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        row, column = (index.row(), index.column()) if index.isValid() else (-1, -1)
        if (row, column) != (self.hover_row, self.hover_column):
            self.hover_row, self.hover_column = row, column
            self.setCursor(Qt.CursorShape.PointingHandCursor if column == COL_CHANNELS else Qt.CursorShape.ArrowCursor)
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hover_row = self.hover_column = -1
        self.viewport().update()
        super().leaveEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Leave:
            self.hover_row = self.hover_column = -1
            self.viewport().update()
        return super().viewportEvent(event)


# ========================================================================== section


class EmptyState(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel("No qualifying members yet")
        self._title.setObjectName("H2")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body = QLabel("Members who joined before the cutoff date will appear here after the first scan.")
        self._body.setObjectName("Muted")
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setWordWrap(True)
        layout.addWidget(self._icon)
        layout.addWidget(self._title)
        layout.addWidget(self._body)
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    def _apply_theme(self, p: Palette) -> None:
        self._icon.setPixmap(icon_pixmap("members", p.text_faint, 34))

    def set_text(self, title: str, body: str) -> None:
        self._title.setText(title)
        self._body.setText(body)


class MemberTableSection(QFrame):
    """Card with 'Members Found' title, toolbar (search/filter/sort/refresh/export) and table."""

    member_activated = Signal(object)
    refresh_requested = Signal()
    export_requested = Signal(object)  # list[MemberRecord]

    def __init__(
        self,
        model: MemberTableModel,
        avatars: AvatarCache,
        title: str = "Members Found",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._model = model
        self.proxy = MemberFilterProxy(self)
        self.proxy.setSourceModel(model)
        self.proxy.set_sort_mode("oldest")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(20, 16, 16, 0)
        title_row.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("H2")
        self._count = QLabel("0")
        self._count.setObjectName("CountBadge")
        title_row.addWidget(heading)
        title_row.addWidget(self._count)
        title_row.addStretch(1)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.setToolTip("Reload the table from the local database")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        title_row.addWidget(self.refresh_button)

        self.export_button = QPushButton("Export CSV")
        self.export_button.setObjectName("SecondaryButton")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip("Export the visible (filtered) members")
        self.export_button.clicked.connect(lambda: self.export_requested.emit(self.proxy.visible_records()))
        title_row.addWidget(self.export_button)
        layout.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(20, 12, 16, 12)
        toolbar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("Search username...")
        self.search.setClearButtonEnabled(True)
        self._search_action = self.search.addAction(themed_icon("search", "#888888", 15), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search, 1)

        filter_label = QLabel("Filter")
        filter_label.setObjectName("Faint")
        toolbar.addSpacing(6)
        toolbar.addWidget(filter_label)
        self.filter = QComboBox()
        self.filter.setToolTip("Filter by join year (UTC)")
        for label, value in FILTERS:
            self.filter.addItem(label, value)
        self.filter.currentIndexChanged.connect(self._on_filter)
        self.filter.setMinimumWidth(130)
        toolbar.addWidget(self.filter)

        sort_label = QLabel("Sort")
        sort_label.setObjectName("Faint")
        toolbar.addSpacing(6)
        toolbar.addWidget(sort_label)
        self.sort = QComboBox()
        self.sort.setToolTip("Sort order")
        for label, value in SORTS:
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self._on_sort)
        self.sort.setMinimumWidth(180)
        toolbar.addWidget(self.sort)
        layout.addLayout(toolbar)

        body = QWidget()
        self._stack = QStackedLayout(body)
        self.table = MemberTableView(avatars)
        self.table.setModel(self.proxy)
        self.table.configure_columns()
        self.table.member_activated.connect(self.member_activated.emit)
        self.empty = EmptyState()
        self._stack.addWidget(self.table)
        self._stack.addWidget(self.empty)
        layout.addWidget(body, 1)

        for signal in (self.proxy.rowsInserted, self.proxy.rowsRemoved, self.proxy.modelReset, self.proxy.layoutChanged):
            signal.connect(self._update_count)
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)
        self._update_count()

    def _apply_theme(self, p: Palette) -> None:
        self.search.removeAction(self._search_action)
        self._search_action = self.search.addAction(themed_icon("search", p.text_faint, 15), QLineEdit.ActionPosition.LeadingPosition)
        self.refresh_button.setIcon(themed_icon("refresh", p.text_muted, 15))
        self.export_button.setIcon(themed_icon("export", p.text_muted, 15))

    def _on_search(self, text: str) -> None:
        self.proxy.set_search(text)
        self._update_count()

    def _on_filter(self, _index: int) -> None:
        self.proxy.set_year_filter(self.filter.currentData())
        self._update_count()

    def _on_sort(self, _index: int) -> None:
        self.proxy.set_sort_mode(self.sort.currentData())
        self.table.viewport().update()

    def _update_count(self, *_args) -> None:
        visible = self.proxy.rowCount()
        total = self._model.rowCount()
        self._count.setText(f"{visible:,}" if visible == total else f"{visible:,} / {total:,}")
        self.export_button.setEnabled(visible > 0)
        if visible:
            self._stack.setCurrentWidget(self.table)
        else:
            if total:
                self.empty.set_text("No matches", "No members match the current search or filter.")
            else:
                self.empty.set_text(
                    "No qualifying members yet",
                    "Members who joined before the cutoff date will appear here after the first scan.",
                )
            self._stack.setCurrentWidget(self.empty)
        # 'No.' column reflects displayed position - repaint after reorder/filter.
        self.table.viewport().update()


# ============================================================================ export


CSV_COLUMNS = [
    "No",
    "Discord User ID",
    "Username",
    "Display Name",
    "Joined Date",
    "Accessible Channel Count",
    "Accessible Channels",
    "First Detected",
    "Last Seen",
]


def export_csv(records: list[MemberRecord], path: Path) -> int:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for number, record in enumerate(records, start=1):
            writer.writerow(
                [
                    number,
                    str(record.user_id),
                    record.username,
                    record.display_name,
                    to_iso(record.joined_at) or "",
                    record.channel_count,
                    "; ".join(c.label for c in record.channels),
                    to_iso(record.first_detected_at) or "",
                    to_iso(record.last_seen_at) or "",
                ]
            )
    return len(records)


def default_export_name(cutoff: datetime) -> str:
    cutoff_label = str(cutoff.year) if (cutoff.month, cutoff.day, cutoff.hour, cutoff.minute) == (1, 1, 0, 0) else cutoff.strftime("%Y-%m-%d")
    return f"discord_members_before_{cutoff_label}_{datetime.now().strftime('%Y-%m-%d')}.csv"
