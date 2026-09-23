"""Members grouped by server: tree model, filter proxy, delegates, section widget, CSV export.

Model layout (``MemberTreeModel``)::

    <server row>  guild A           (top level, one per configured Guild ID)
        <member>  user1             (children: only members stored under guild A)
        <member>  user2
    <server row>  guild B
        <member>  user3

Each server node owns its own member list, which is replaced only by
``set_members(guild_id, records)`` for that guild. So a member can only ever be
displayed under the server it was fetched from.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (
    QAbstractItemModel,
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
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.avatars import AvatarCache
from app.models import GUILD_STATUS_LABEL, GuildState, GuildStatus, MemberRecord
from app.utils import format_clock, format_date, format_precise, to_iso
from ui.components.avatar import fallback_color, initials, paint_avatar
from ui.components.icons import icon_pixmap, themed_icon
from ui.theme import MONO_FAMILIES, Palette, ThemeManager

(
    COL_NO,
    COL_USER,
    COL_DISPLAY,
    COL_ID,
    COL_JOINED,
    COL_BOT,
    COL_ROLES,
    COL_CHANNELS,
) = range(8)
HEADERS = ["NO.", "USERNAME", "DISPLAY NAME", "USER ID", "JOINED DATE", "BOT", "ROLES", "CHANNELS"]
RecordRole = Qt.ItemDataRole.UserRole + 1
GuildRole = Qt.ItemDataRole.UserRole + 2
ROW_HEIGHT = 52

SORTS = [
    ("Oldest joined", "oldest"),
    ("Newest joined", "newest"),
    ("Username A-Z", "az"),
    ("Username Z-A", "za"),
    ("Recently discovered", "recent"),
]

_STATUS_TOKEN = {
    GuildStatus.PENDING: "text_faint",
    GuildStatus.AVAILABLE: "accent",
    GuildStatus.LOADING: "warning",
    GuildStatus.SYNCED: "success",
    GuildStatus.ERROR: "danger",
    GuildStatus.NOT_MEMBER: "danger",
    GuildStatus.UNAVAILABLE: "warning",
}


def year_filters() -> list[tuple[str, object]]:
    current = datetime.now(timezone.utc).year
    return [("All years", None), ("Before 2020", "before2020")] + [(str(y), y) for y in range(2020, current + 1)]


# ============================================================================ model


class _GuildNode:
    __slots__ = ("node_id", "guild_id", "records", "index")

    def __init__(self, node_id: int, guild_id: int) -> None:
        self.node_id = node_id
        self.guild_id = guild_id
        self.records: list[MemberRecord] = []
        self.index: dict[int, int] = {}

    def reindex(self) -> None:
        self.index = {r.user_id: i for i, r in enumerate(self.records)}


class MemberTreeModel(QAbstractItemModel):
    """Two-level model: servers (top level) -> that server's members."""

    def __init__(self, state_provider: Callable[[int], GuildState | None], parent=None) -> None:
        super().__init__(parent)
        self._state_provider = state_provider
        self._order: list[_GuildNode] = []
        self._by_id: dict[int, _GuildNode] = {}       # node_id -> node
        self._by_guild: dict[int, _GuildNode] = {}    # guild_id -> node
        self._next_node_id = 1
        self._avatar_rows: dict[str, list[tuple[int, int]]] = {}

    # --- structure
    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:  # noqa: D401
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, 0)
        node = self._order[parent.row()]
        return self.createIndex(row, column, node.node_id)

    def parent(self, index=QModelIndex()) -> QModelIndex:  # type: ignore[override]
        if not index.isValid() or index.internalId() == 0:
            return QModelIndex()
        node = self._by_id.get(index.internalId())
        if node is None:
            return QModelIndex()
        return self.createIndex(self._order.index(node), 0, 0)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._order)
        if parent.internalId() != 0 or parent.column() != 0:
            return 0
        return len(self._order[parent.row()].records)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return HEADERS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if index.internalId() == 0:
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # --- data
    def node_at(self, row: int) -> _GuildNode | None:
        return self._order[row] if 0 <= row < len(self._order) else None

    def guild_row(self, guild_id: int) -> int:
        node = self._by_guild.get(guild_id)
        return self._order.index(node) if node is not None else -1

    def record_for(self, index: QModelIndex) -> MemberRecord | None:
        if not index.isValid() or index.internalId() == 0:
            return None
        node = self._by_id.get(index.internalId())
        if node is None or not (0 <= index.row() < len(node.records)):
            return None
        return node.records[index.row()]

    def guild_id_for(self, index: QModelIndex) -> int | None:
        if not index.isValid():
            return None
        if index.internalId() == 0:
            node = self.node_at(index.row())
        else:
            node = self._by_id.get(index.internalId())
        return node.guild_id if node else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if index.internalId() == 0:
            node = self.node_at(index.row())
            if node is None:
                return None
            state = self._state_provider(node.guild_id)
            if role == GuildRole:
                return state
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                return state.display_name if state else str(node.guild_id)
            return None

        record = self.record_for(index)
        if record is None:
            return None
        column = index.column()
        if role == RecordRole:
            return record
        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_USER:
                return record.username
            if column == COL_DISPLAY:
                return record.display_name
            if column == COL_ID:
                return str(record.user_id)
            if column == COL_JOINED:
                return format_date(record.joined_at)
            if column == COL_BOT:
                return "Bot" if record.is_bot else "No"
            if column == COL_ROLES:
                return record.role_names or "—"
            if column == COL_CHANNELS:
                return f"{record.channel_count} channels"
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_JOINED:
                return format_precise(record.joined_at)
            if column == COL_ROLES and record.roles:
                return "\n".join(r.name for r in record.roles)
            if column == COL_CHANNELS:
                return "Channels this member can view (based on current permissions). Click for details."
            if column == COL_USER and record.shows_display_name:
                return f"{record.username} · {record.display_name}"
        return None

    # --- updates
    def set_guilds(self, guild_ids: list[int]) -> None:
        """Set the configured servers (in display order), keeping loaded members of kept servers."""
        if [n.guild_id for n in self._order] == list(guild_ids):
            return
        self.beginResetModel()
        order: list[_GuildNode] = []
        for gid in guild_ids:
            node = self._by_guild.get(gid)
            if node is None:
                node = _GuildNode(self._next_node_id, gid)
                self._next_node_id += 1
            order.append(node)
        self._order = order
        self._by_guild = {n.guild_id: n for n in order}
        self._by_id = {n.node_id: n for n in order}
        self._rebuild_avatar_index()
        self.endResetModel()

    def set_members(self, guild_id: int, records: list[MemberRecord]) -> None:
        """Replace the member list of ONE server with minimal churn."""
        node = self._by_guild.get(guild_id)
        if node is None:
            return
        records = [r for r in records if r.guild_id == guild_id]  # hard guarantee: no cross-server rows
        parent = self.createIndex(self._order.index(node), 0, 0)
        wanted = {r.user_id for r in records}

        # 1) removals (members that left / were filtered out), bottom-up in contiguous ranges
        remove_rows = [i for i, r in enumerate(node.records) if r.user_id not in wanted]
        for start, end in reversed(list(_ranges(remove_rows))):
            self.beginRemoveRows(parent, start, end)
            del node.records[start : end + 1]
            self.endRemoveRows()
        if remove_rows:
            node.reindex()

        # 2) in-place updates
        new_records: list[MemberRecord] = []
        for record in records:
            row = node.index.get(record.user_id)
            if row is None:
                new_records.append(record)
            elif node.records[row] != record:
                node.records[row] = record
                self.dataChanged.emit(self.index(row, 0, parent), self.index(row, len(HEADERS) - 1, parent))

        # 3) insertions
        if new_records:
            start = len(node.records)
            self.beginInsertRows(parent, start, start + len(new_records) - 1)
            node.records.extend(new_records)
            self.endInsertRows()
        node.reindex()
        self._rebuild_avatar_index()

    def refresh_guild(self, guild_id: int) -> None:
        row = self.guild_row(guild_id)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))

    def refresh_avatar(self, url: str) -> None:
        for guild_row, row in self._avatar_rows.get(url, []):
            node = self.node_at(guild_row)
            if node is None or row >= len(node.records):
                continue
            idx = self.createIndex(row, COL_USER, node.node_id)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
        for node in self._order:
            state = self._state_provider(node.guild_id)
            if state is not None and state.icon_url == url:
                self.refresh_guild(node.guild_id)

    def total_members(self) -> int:
        return sum(len(n.records) for n in self._order)

    def _rebuild_avatar_index(self) -> None:
        self._avatar_rows = {}
        for guild_row, node in enumerate(self._order):
            for row, record in enumerate(node.records):
                if record.avatar_url:
                    self._avatar_rows.setdefault(record.avatar_url, []).append((guild_row, row))


def _ranges(rows: list[int]):
    """Group sorted row numbers into contiguous (start, end) ranges."""
    start = prev = None
    for row in rows:
        if start is None:
            start = prev = row
        elif row == prev + 1:
            prev = row
        else:
            yield start, prev
            start = prev = row
    if start is not None:
        yield start, prev


class MemberFilterProxy(QSortFilterProxyModel):
    """Server selection, search, year and cutoff filtering; per-server numbering and sorting."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._guild: int | None = None
        self._search = ""
        self._year: int | str | None = None
        self._cutoff: datetime | None = None
        self._ignore_bots = False
        self._sort_mode = "oldest"
        self.setRecursiveFilteringEnabled(True)
        self.setDynamicSortFilter(True)

    # --- configuration
    @property
    def selected_guild(self) -> int | None:
        return self._guild

    def set_guild(self, guild_id: int | None) -> None:
        if guild_id != self._guild:
            self._guild = guild_id
            self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._search = text.strip().casefold()
        self.invalidateFilter()

    def set_year(self, value) -> None:
        self._year = value
        self.invalidateFilter()

    def set_qualifying(self, cutoff: datetime | None, ignore_bots: bool) -> None:
        """Only show members that joined before ``cutoff`` (None = show everyone)."""
        self._cutoff = cutoff
        self._ignore_bots = ignore_bots
        self.invalidateFilter()

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode
        self.invalidate()
        self.sort(0, Qt.SortOrder.AscendingOrder)

    @property
    def text_filters_active(self) -> bool:
        return bool(self._search) or self._year is not None

    # --- filtering
    def _model(self) -> MemberTreeModel:
        return self.sourceModel()  # type: ignore[return-value]

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        model = self._model()
        if not source_parent.isValid():
            node = model.node_at(source_row)
            if node is None or (self._guild is not None and node.guild_id != self._guild):
                return False
            # With a search/year filter the header only shows when a member matches
            # (recursive filtering accepts parents of accepted children).
            return not self.text_filters_active

        node = model.node_at(source_parent.row())
        if node is None or (self._guild is not None and node.guild_id != self._guild):
            return False
        if not (0 <= source_row < len(node.records)):
            return False
        record = node.records[source_row]
        if record.guild_id != node.guild_id:
            return False
        if self._cutoff is not None:
            if record.joined_at is None or record.joined_at >= self._cutoff or not record.in_guild:
                return False
            if self._ignore_bots and record.is_bot:
                return False
        if self._search:
            haystack = f"{record.username}\n{record.display_name}\n{record.user_id}".casefold()
            if self._search not in haystack:
                return False
        if self._year is not None:
            if record.joined_at is None:
                return False
            year = record.joined_at.year  # stored as UTC, like the cutoff
            if self._year == "before2020":
                if year >= 2020:
                    return False
            elif year != self._year:
                return False
        return True

    def lessThan(self, left, right) -> bool:  # noqa: N802
        model = self._model()
        if not left.parent().isValid():
            return left.row() < right.row()  # servers keep the configured order
        a = model.record_for(left)
        b = model.record_for(right)
        if a is None or b is None:
            return left.row() < right.row()
        mode = self._sort_mode
        far = datetime.max
        if mode in ("oldest", "newest"):
            ka = (a.joined_at.replace(tzinfo=None) if a.joined_at else far, a.user_id)
            kb = (b.joined_at.replace(tzinfo=None) if b.joined_at else far, b.user_id)
            return ka < kb if mode == "oldest" else ka > kb
        if mode in ("az", "za"):
            ka = (a.username.casefold(), a.user_id)
            kb = (b.username.casefold(), b.user_id)
            return ka < kb if mode == "az" else ka > kb
        ka = (a.first_detected_at.replace(tzinfo=None) if a.first_detected_at else datetime.min, a.user_id)
        kb = (b.first_detected_at.replace(tzinfo=None) if b.first_detected_at else datetime.min, b.user_id)
        return ka > kb

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if (
            index.isValid()
            and index.column() == COL_NO
            and role == Qt.ItemDataRole.DisplayRole
            and index.parent().isValid()
        ):
            return str(index.row() + 1)  # numbering restarts for every server
        return super().data(index, role)

    # --- helpers
    def visible_member_count(self) -> int:
        return sum(self.rowCount(self.index(row, 0)) for row in range(self.rowCount()))

    def visible_count_for(self, guild_id: int) -> int:
        for row in range(self.rowCount()):
            idx = self.index(row, 0)
            if self._model().guild_id_for(self.mapToSource(idx)) == guild_id:
                return self.rowCount(idx)
        return 0

    def visible_groups(self) -> list[tuple[GuildState | None, list[MemberRecord]]]:
        """Visible members grouped by server, in display order (used for CSV export)."""
        groups = []
        for row in range(self.rowCount()):
            parent = self.index(row, 0)
            state = parent.data(GuildRole)
            records = [self.index(r, 0, parent).data(RecordRole) for r in range(self.rowCount(parent))]
            groups.append((state, [r for r in records if r is not None]))
        return groups


# ======================================================================== delegates


class _BaseDelegate(QStyledItemDelegate):
    """Paints the row background (hover/selection) and bottom separator for member cells."""

    def __init__(self, view: "MemberTreeView") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(super().sizeHint(option, index).width(), ROW_HEIGHT)

    def paint_background(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        p = ThemeManager.instance().palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = self._view.is_hovered(index)
        painter.save()
        if selected:
            painter.fillRect(rect, QColor(p.selection))
        elif hovered:
            painter.fillRect(rect, QColor(p.hover))
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()

    @staticmethod
    def record(index) -> MemberRecord | None:
        return index.data(RecordRole)

    @staticmethod
    def _dim(painter: QPainter, record: MemberRecord | None) -> None:
        if record is not None and not record.in_guild:
            painter.setOpacity(0.55)


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
        self._dim(painter, record)
        painter.setPen(QColor(p.text_faint if self._muted else p.text))
        font = QFont(option.font)
        if self._mono:
            font.setFamilies(MONO_FAMILIES)
            font.setPixelSize(12)
        painter.setFont(font)
        rect = option.rect.adjusted(14, 0, -10, 0)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        text = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


class UserDelegate(_BaseDelegate):
    AVATAR = 30

    def __init__(self, view, avatars: AvatarCache) -> None:
        super().__init__(view)
        self._avatars = avatars
        self.show_display_name = True  # sub-line; off when the Display Name column is visible

    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._dim(painter, record)
        avatar_rect = QRectF(rect.left() + 12, rect.center().y() - self.AVATAR / 2 + 1, self.AVATAR, self.AVATAR)
        paint_avatar(
            painter, avatar_rect, self._avatars, record.avatar_url, record.user_id, record.username,
            self._view.devicePixelRatioF(),
        )
        text_left = int(avatar_rect.right()) + 11
        text_width = rect.right() - text_left - 10
        name_font = QFont(option.font)
        name_font.setWeight(QFont.Weight.DemiBold)
        sub_font = QFont(option.font)
        sub_font.setPixelSize(12)
        name_metrics = QFontMetrics(name_font)
        sub_metrics = QFontMetrics(sub_font)
        show_sub = self.show_display_name and record.shows_display_name
        if show_sub:
            total = name_metrics.height() + sub_metrics.height()
            top = rect.center().y() - total // 2
            name_rect = QRect(text_left, top, text_width, name_metrics.height())
            sub_rect = QRect(text_left, top + name_metrics.height(), text_width, sub_metrics.height())
        else:
            name_rect = QRect(text_left, rect.top(), text_width, rect.height())
            sub_rect = None

        tag = "LEFT" if not record.in_guild else ""
        tag_width = QFontMetrics(sub_font).horizontalAdvance(tag) + 14 if tag else 0
        painter.setFont(name_font)
        painter.setPen(QColor(p.text))
        name = name_metrics.elidedText(record.username, Qt.TextElideMode.ElideRight, max(20, text_width - tag_width))
        painter.drawText(name_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), name)
        if sub_rect is not None:
            painter.setFont(sub_font)
            painter.setPen(QColor(p.text_muted))
            sub = sub_metrics.elidedText(record.display_name, Qt.TextElideMode.ElideRight, text_width)
            painter.drawText(sub_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), sub)
        if tag:
            painter.setOpacity(1.0)
            tag_font = QFont(option.font)
            tag_font.setPixelSize(10)
            tag_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(tag_font)
            used = name_metrics.horizontalAdvance(name)
            tag_rect = QRectF(name_rect.left() + used + 8, name_rect.center().y() - 8, QFontMetrics(tag_font).horizontalAdvance(tag) + 12, 16)
            if tag_rect.right() < rect.right() - 4:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(p.warning_soft))
                painter.drawRoundedRect(tag_rect, 8, 8)
                painter.setPen(QColor(p.warning))
                painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag)
        painter.restore()


class BotDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._dim(painter, record)
        font = QFont(option.font)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        if record.is_bot:
            rect = QRectF(option.rect.left() + 12, option.rect.center().y() - 10, 40, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.accent_soft))
            painter.drawRoundedRect(rect, 10, 10)
            painter.setPen(QColor(p.accent))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "BOT")
        else:
            painter.setPen(QColor(p.text_faint))
            painter.drawText(option.rect.adjusted(14, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "No")
        painter.restore()


class RolesDelegate(_BaseDelegate):
    """Role chips coloured like in Discord (up to what fits, then '+N')."""

    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._dim(painter, record)
        font = QFont(option.font)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Medium)
        fm = QFontMetrics(font)
        painter.setFont(font)
        x = option.rect.left() + 12
        right = option.rect.right() - 8
        y = option.rect.center().y() - 10
        if not record.roles:
            painter.setPen(QColor(p.text_faint))
            painter.drawText(option.rect.adjusted(14, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "—")
            painter.restore()
            return
        for i, role in enumerate(record.roles):
            label = fm.elidedText(role.name, Qt.TextElideMode.ElideRight, 110)
            width = fm.horizontalAdvance(label) + 26
            remaining = len(record.roles) - i
            more_width = fm.horizontalAdvance(f"+{remaining}") + 14
            if x + width > right - (more_width if remaining > 1 else 0):
                chip = QRectF(x, y, more_width, 20)
                painter.setPen(QPen(QColor(p.border), 1))
                painter.setBrush(QColor(p.surface_alt))
                painter.drawRoundedRect(chip, 10, 10)
                painter.setPen(QColor(p.text_muted))
                painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, f"+{remaining}")
                break
            chip = QRectF(x, y, width, 20)
            painter.setPen(QPen(QColor(p.border), 1))
            painter.setBrush(QColor(p.surface_alt))
            painter.drawRoundedRect(chip, 10, 10)
            dot = QColor(role.hex_color or p.text_faint)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot)
            painter.drawEllipse(QRectF(x + 8, y + 7, 6, 6))
            painter.setPen(QColor(p.text))
            painter.drawText(QRectF(x + 18, y, width - 22, 20), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
            x += width + 5
        painter.restore()


class ChannelsDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        record = self.record(index)
        if record is None:
            return
        p = ThemeManager.instance().palette
        hovered = self._view.is_hovered(index) and self._view.hover_column == COL_CHANNELS
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._dim(painter, record)
        font = QFont(option.font)
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        text = f"{record.channel_count} channel{'s' if record.channel_count != 1 else ''}"
        width = QFontMetrics(font).horizontalAdvance(text) + 38
        rect = QRectF(option.rect.left() + 12, option.rect.center().y() - 12, min(width, option.rect.width() - 16), 24)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.fillPath(path, QColor(p.accent_soft if hovered else p.surface_alt))
        painter.setPen(QPen(QColor(p.accent if hovered else p.border), 1))
        painter.drawPath(path)
        painter.drawPixmap(int(rect.left() + 9), int(rect.center().y() - 6.5), icon_pixmap("hash", p.accent if hovered else p.text_muted, 13))
        painter.setPen(QColor(p.accent if hovered else p.text))
        painter.drawText(rect.adjusted(26, 0, -8, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()


# ============================================================================= view


class MemberTreeView(QTreeView):
    member_activated = Signal(object)  # MemberRecord

    def __init__(self, avatars: AvatarCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MemberTable")
        self._avatars = avatars
        self._hover: QPersistentModelIndex = QPersistentModelIndex()
        self.hover_column = -1
        self._collapsed: set[int] = set()
        self.setMouseTracking(True)
        self.setUniformRowHeights(True)
        self.setIndentation(0)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(True)
        self.setExpandsOnDoubleClick(False)
        self.setAnimated(False)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        header = self.header()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        header.setSectionsMovable(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumHeight(36)
        header.setStretchLastSection(False)

        self.user_delegate = UserDelegate(self, avatars)
        self.setItemDelegateForColumn(COL_NO, TextDelegate(self, muted=True, mono=True))
        self.setItemDelegateForColumn(COL_USER, self.user_delegate)
        self.setItemDelegateForColumn(COL_DISPLAY, TextDelegate(self))
        self.setItemDelegateForColumn(COL_ID, TextDelegate(self, muted=True, mono=True))
        self.setItemDelegateForColumn(COL_JOINED, TextDelegate(self))
        self.setItemDelegateForColumn(COL_BOT, BotDelegate(self))
        self.setItemDelegateForColumn(COL_ROLES, RolesDelegate(self))
        self.setItemDelegateForColumn(COL_CHANNELS, ChannelsDelegate(self))

        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)
        ThemeManager.instance().changed.connect(lambda _p: self.viewport().update())

    def setModel(self, model) -> None:  # noqa: N802
        super().setModel(model)
        model.rowsInserted.connect(self._on_rows_inserted)
        model.modelReset.connect(self.apply_expansion)
        model.layoutChanged.connect(self.apply_expansion)
        self.apply_expansion()

    def configure_columns(self, visible: list[int]) -> None:
        header = self.header()
        widths = {COL_NO: 60, COL_USER: 230, COL_DISPLAY: 170, COL_ID: 180, COL_JOINED: 130, COL_BOT: 70, COL_ROLES: 240, COL_CHANNELS: 140}
        for col in range(len(HEADERS)):
            self.setColumnHidden(col, col not in visible)
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(col, widths[col])
        stretch = COL_ROLES if COL_ROLES in visible else COL_USER
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
        self.user_delegate.show_display_name = COL_DISPLAY not in visible
        self.viewport().update()

    # --- expansion (servers are expanded unless the user collapsed them)
    def _guild_id(self, proxy_index) -> int | None:
        model = self.model()
        if model is None or not proxy_index.isValid():
            return None
        source = model.mapToSource(proxy_index) if isinstance(model, QSortFilterProxyModel) else proxy_index
        tree = model.sourceModel() if isinstance(model, QSortFilterProxyModel) else model
        return tree.guild_id_for(source)

    def apply_expansion(self) -> None:
        model = self.model()
        if model is None:
            return
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            gid = self._guild_id(idx)
            self.setExpanded(idx, gid not in self._collapsed)

    def _on_rows_inserted(self, parent, _first, _last) -> None:
        if not parent.isValid():
            self.apply_expansion()

    def toggle_guild(self, proxy_index) -> None:
        gid = self._guild_id(proxy_index)
        if gid is None:
            return
        if gid in self._collapsed:
            self._collapsed.discard(gid)
        else:
            self._collapsed.add(gid)
        self.setExpanded(proxy_index, gid not in self._collapsed)

    # --- painting
    def drawBranches(self, painter, rect, index) -> None:  # noqa: N802 - no tree decorations
        return

    def drawRow(self, painter: QPainter, option, index) -> None:  # noqa: N802
        if index.parent().isValid():
            super().drawRow(painter, option, index)
            return
        self._paint_guild_header(painter, option.rect, index)

    def _paint_guild_header(self, painter: QPainter, rect: QRect, index) -> None:
        p = ThemeManager.instance().palette
        state: GuildState | None = index.data(GuildRole)
        rect = QRect(0, rect.top(), self.viewport().width(), rect.height())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, QColor(p.surface_alt))
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if state is None:
            painter.restore()
            return
        status_color = QColor(getattr(p, _STATUS_TOKEN.get(state.status, "text_faint")))
        painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor(p.accent))

        icon_rect = QRectF(rect.left() + 14, rect.center().y() - 15, 30, 30)
        pixmap = self._avatars.rounded(state.icon_url, 30, self.devicePixelRatioF()) if state.icon_url else None
        if pixmap is not None:
            painter.drawPixmap(icon_rect.toRect(), pixmap)
        else:
            path = QPainterPath()
            path.addRoundedRect(icon_rect, 9, 9)
            painter.fillPath(path, fallback_color(state.guild_id))
            font = QFont(self.font())
            font.setPixelSize(13)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, initials(state.display_name))

        left = int(icon_rect.right()) + 12
        title_font = QFont(self.font())
        title_font.setPixelSize(14)
        title_font.setWeight(QFont.Weight.Bold)
        meta_font = QFont(self.font())
        meta_font.setPixelSize(11)
        tf, mf = QFontMetrics(title_font), QFontMetrics(meta_font)
        top = rect.center().y() - (tf.height() + mf.height()) // 2

        # right side: chevron
        collapsed = not self.isExpanded(index)
        chevron = icon_pixmap("chevron-down", p.text_muted, 16)
        painter.save()
        cx, cy = rect.right() - 22, rect.center().y()
        painter.translate(cx, cy)
        if collapsed:
            painter.rotate(-90)
        painter.drawPixmap(-8, -8, chevron)
        painter.restore()

        # title + status pill
        painter.setFont(title_font)
        painter.setPen(QColor(p.text))
        available = rect.right() - 60 - left
        title = tf.elidedText(f"Server: {state.display_name}", Qt.TextElideMode.ElideRight, max(80, available - 140))
        painter.drawText(QRect(left, top, available, tf.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        pill_text = GUILD_STATUS_LABEL.get(state.status, "")
        pill_font = QFont(self.font())
        pill_font.setPixelSize(10)
        pill_font.setWeight(QFont.Weight.DemiBold)
        pf = QFontMetrics(pill_font)
        pill = QRectF(left + tf.horizontalAdvance(title) + 10, top + (tf.height() - 18) / 2, pf.horizontalAdvance(pill_text) + 16, 18)
        soft = QColor(status_color)
        soft.setAlphaF(0.16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(soft)
        painter.drawRoundedRect(pill, 9, 9)
        painter.setFont(pill_font)
        painter.setPen(status_color)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, pill_text)

        # meta line
        model = self.model()
        shown = model.rowCount(index) if model is not None else 0
        parts = [f"Guild ID {state.guild_id}"]
        if state.member_count is not None:
            parts.append(f"{state.member_count:,} members")
        parts.append(f"{state.loaded:,} loaded")
        parts.append(f"{shown:,} shown")
        if state.status == GuildStatus.LOADING and state.message:
            parts.append(state.message)
        elif state.status in (GuildStatus.ERROR, GuildStatus.NOT_MEMBER, GuildStatus.UNAVAILABLE) and state.message:
            parts.append(state.message)
        elif state.last_synced_at:
            parts.append(f"Synced {format_clock(state.last_synced_at)}")
        painter.setFont(meta_font)
        painter.setPen(QColor(p.danger) if state.status in (GuildStatus.ERROR, GuildStatus.NOT_MEMBER) else QColor(p.text_muted))
        meta = mf.elidedText("  ·  ".join(parts), Qt.TextElideMode.ElideRight, available)
        painter.drawText(QRect(left, top + tf.height(), available, mf.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), meta)

        # loading progress
        if state.status == GuildStatus.LOADING:
            bar = QRectF(rect.left(), rect.bottom() - 2, rect.width(), 2)
            painter.fillRect(bar, QColor(p.border))
            fraction = state.progress if state.progress is not None else 0.15
            painter.fillRect(QRectF(bar.left(), bar.top(), bar.width() * max(0.02, fraction), 2), status_color)
        painter.restore()

    # --- interaction
    def is_hovered(self, index) -> bool:
        return self._hover.isValid() and index.row() == self._hover.row() and index.parent() == self._hover.parent()

    def _record(self, index) -> MemberRecord | None:
        return index.data(RecordRole) if index.isValid() and index.parent().isValid() else None

    def _on_clicked(self, index) -> None:
        if not index.parent().isValid():
            self.toggle_guild(index.siblingAtColumn(0))
            return
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
        column = index.column() if index.isValid() else -1
        target = QPersistentModelIndex(index.siblingAtColumn(0)) if index.isValid() and index.parent().isValid() else QPersistentModelIndex()
        if target != self._hover or column != self.hover_column:
            self._hover = target
            self.hover_column = column
            clickable = (index.isValid() and not index.parent().isValid()) or (target.isValid() and column == COL_CHANNELS)
            self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)
            self.viewport().update()
        super().mouseMoveEvent(event)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Leave:
            self._hover = QPersistentModelIndex()
            self.hover_column = -1
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
        self._title = QLabel()
        self._title.setObjectName("H2")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body = QLabel()
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


class MemberSection(QFrame):
    """Card: title, refresh/export actions, server picker + search/filter/sort, grouped table."""

    member_activated = Signal(object)
    refresh_guild_requested = Signal(object)
    refresh_all_requested = Signal()
    export_requested = Signal(object, str)   # visible groups, scope label for the file name
    guild_selected = Signal(object)          # guild id | None

    def __init__(
        self,
        model: MemberTreeModel,
        avatars: AvatarCache,
        title: str,
        columns: list[int],
        qualifying_only: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._model = model
        self._qualifying_only = qualifying_only
        self._guild_names: dict[int, str] = {}
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
        self.refresh_one = QPushButton("Refresh Server")
        self.refresh_one.setToolTip("Re-fetch the members of the selected server from Discord")
        self.refresh_all = QPushButton("Refresh All")
        self.refresh_all.setToolTip("Re-fetch the members of every configured server")
        self.export_button = QPushButton("Export CSV")
        self.export_button.setToolTip("Export the visible members, grouped by server")
        for button in (self.refresh_one, self.refresh_all, self.export_button):
            button.setObjectName("SecondaryButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            title_row.addWidget(button)
        self.refresh_one.clicked.connect(self._on_refresh_one)
        self.refresh_all.clicked.connect(self.refresh_all_requested.emit)
        self.export_button.clicked.connect(self._on_export)
        layout.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(20, 12, 16, 12)
        toolbar.setSpacing(8)
        self.server = QComboBox()
        self.server.setToolTip("Show members of one server")
        self.server.setMinimumWidth(190)
        self.server.currentIndexChanged.connect(self._on_server)
        toolbar.addWidget(self.server)

        self.search = QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("Search username, display name or user ID...")
        self.search.setClearButtonEnabled(True)
        self._search_action = self.search.addAction(themed_icon("search", "#888888", 15), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search, 1)

        self.year = QComboBox()
        self.year.setToolTip("Filter by join year (UTC)")
        for label, value in year_filters():
            self.year.addItem(label, value)
        self.year.currentIndexChanged.connect(lambda _i: (self.proxy.set_year(self.year.currentData()), self._update_state()))
        self.year.setMinimumWidth(120)
        toolbar.addWidget(self.year)

        self.sort = QComboBox()
        self.sort.setToolTip("Sort members within each server")
        for label, value in SORTS:
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self._on_sort)
        self.sort.setMinimumWidth(170)
        toolbar.addWidget(self.sort)
        layout.addLayout(toolbar)

        body = QWidget()
        self._stack = QStackedLayout(body)
        self.tree = MemberTreeView(avatars)
        self.tree.setModel(self.proxy)
        self.tree.configure_columns(columns)
        self.tree.member_activated.connect(self.member_activated.emit)
        self.empty = EmptyState()
        self._stack.addWidget(self.tree)
        self._stack.addWidget(self.empty)
        layout.addWidget(body, 1)

        for signal in (
            self.proxy.rowsInserted,
            self.proxy.rowsRemoved,
            self.proxy.modelReset,
            self.proxy.layoutChanged,
        ):
            signal.connect(self._update_state)
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)
        self._update_state()

    # --- public
    def set_guilds(self, states: list[GuildState]) -> None:
        """Rebuild the server picker (keeps the current selection when possible)."""
        current = self.proxy.selected_guild
        self._guild_names = {s.guild_id: s.display_name for s in states}
        self.server.blockSignals(True)
        self.server.clear()
        self.server.addItem(f"All Servers ({len(states)})", None)
        for state in states:
            self.server.addItem(state.display_name, state.guild_id)
            self.server.setItemData(self.server.count() - 1, f"Guild ID {state.guild_id}", Qt.ItemDataRole.ToolTipRole)
        index = self.server.findData(current) if current is not None else 0
        self.server.setCurrentIndex(max(0, index))
        self.server.blockSignals(False)
        if index < 0:
            self._select(None)
        self._update_state()

    def update_guild_name(self, state: GuildState) -> None:
        index = self.server.findData(state.guild_id)
        if index > 0 and self.server.itemText(index) != state.display_name:
            self.server.setItemText(index, state.display_name)
        self._guild_names[state.guild_id] = state.display_name

    def set_selected_guild(self, guild_id: int | None) -> None:
        index = self.server.findData(guild_id) if guild_id is not None else 0
        if index >= 0 and index != self.server.currentIndex():
            self.server.setCurrentIndex(index)  # triggers _on_server

    def set_cutoff(self, cutoff: datetime, ignore_bots: bool) -> None:
        if self._qualifying_only:
            self.proxy.set_qualifying(cutoff, ignore_bots)
            self._update_state()

    # --- internal
    def _apply_theme(self, p: Palette) -> None:
        self.search.removeAction(self._search_action)
        self._search_action = self.search.addAction(themed_icon("search", p.text_faint, 15), QLineEdit.ActionPosition.LeadingPosition)
        self.refresh_one.setIcon(themed_icon("refresh", p.text_muted, 15))
        self.refresh_all.setIcon(themed_icon("refresh", p.text_muted, 15))
        self.export_button.setIcon(themed_icon("export", p.text_muted, 15))

    def _select(self, guild_id: int | None) -> None:
        self.proxy.set_guild(guild_id)
        self.tree.apply_expansion()
        self.guild_selected.emit(guild_id)
        self._update_state()

    def _on_server(self, _index: int) -> None:
        self._select(self.server.currentData())

    def _on_search(self, text: str) -> None:
        self.proxy.set_search(text)
        self.tree.apply_expansion()
        self._update_state()

    def _on_sort(self, _index: int) -> None:
        self.proxy.set_sort_mode(self.sort.currentData())
        self.tree.apply_expansion()
        self.tree.viewport().update()

    def _on_refresh_one(self) -> None:
        gid = self.proxy.selected_guild
        if gid is not None:
            self.refresh_guild_requested.emit(gid)

    def _on_export(self) -> None:
        gid = self.proxy.selected_guild
        scope = self._guild_names.get(gid, str(gid)) if gid is not None else "all_servers"
        self.export_requested.emit(self.proxy.visible_groups(), scope)

    def _update_state(self, *_args) -> None:
        visible = self.proxy.visible_member_count()
        total = self._model.total_members() if self.proxy.selected_guild is None else self._count_in_guild(self.proxy.selected_guild)
        filtered = self.proxy.text_filters_active or self._qualifying_only
        self._count.setText(f"{visible:,} / {total:,}" if filtered and visible != total else f"{visible:,}")
        self.export_button.setEnabled(visible > 0)
        self.refresh_one.setEnabled(self.proxy.selected_guild is not None)
        if self.proxy.rowCount():
            self._stack.setCurrentWidget(self.tree)
        else:
            if self._model.rowCount() == 0:
                self.empty.set_text("No servers configured", "Add one or more Server (Guild) IDs in Settings.")
            elif self.proxy.text_filters_active:
                self.empty.set_text("No matches", "No members in the selected server match the search or filter.")
            else:
                self.empty.set_text("No members yet", "Members appear here after the first sync.")
            self._stack.setCurrentWidget(self.empty)
        self.tree.viewport().update()

    def _count_in_guild(self, guild_id: int) -> int:
        row = self._model.guild_row(guild_id)
        node = self._model.node_at(row)
        return len(node.records) if node else 0


# ============================================================================ export


CSV_COLUMNS = [
    "No",
    "Guild ID",
    "Server Name",
    "Discord User ID",
    "Username",
    "Display Name",
    "Bot",
    "Roles",
    "Joined Date",
    "In Server",
    "Accessible Channel Count",
    "Accessible Channels",
    "First Detected",
    "Last Seen",
]


def export_csv(groups: list[tuple[GuildState | None, list[MemberRecord]]], path: Path) -> int:
    """Write members grouped by server; 'No' restarts at 1 for every server."""
    count = 0
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for state, records in groups:
            for number, record in enumerate(records, start=1):
                server_name = (state.display_name if state else "") or record.guild_name
                writer.writerow(
                    [
                        number,
                        str(record.guild_id),
                        server_name,
                        str(record.user_id),
                        record.username,
                        record.display_name,
                        "Yes" if record.is_bot else "No",
                        "; ".join(r.name for r in record.roles),
                        to_iso(record.joined_at) or "",
                        "Yes" if record.in_guild else "No",
                        record.channel_count,
                        "; ".join(c.label for c in record.channels),
                        to_iso(record.first_detected_at) or "",
                        to_iso(record.last_seen_at) or "",
                    ]
                )
                count += 1
    return count


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return slug[:40] or "server"


def default_export_name(scope: str, cutoff: datetime | None) -> str:
    """e.g. discord_members_before_2024_all_servers_2026-09-22.csv"""
    parts = ["discord_members"]
    if cutoff is not None:
        label = str(cutoff.year) if (cutoff.month, cutoff.day, cutoff.hour, cutoff.minute) == (1, 1, 0, 0) else cutoff.strftime("%Y-%m-%d")
        parts.append(f"before_{label}")
    parts.append(_slug(scope))
    parts.append(datetime.now().strftime("%Y-%m-%d"))
    return "_".join(parts) + ".csv"
