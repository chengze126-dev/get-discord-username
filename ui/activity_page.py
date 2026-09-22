"""Activity page: full activity log and scan history."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.utils import format_clock, format_duration_ms, from_iso
from ui.dashboard import ActivityDelegate, ActivityModel, PageHeader


class ScanHistoryModel(QAbstractTableModel):
    HEADERS = ["Started", "Status", "Checked", "Matches", "New", "Duration", "Details"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section].upper()
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            started = from_iso(row["started_at"])
            values = [
                f"{started.astimezone().strftime('%b %d')}  {format_clock(started)}" if started else "—",
                row["status"].capitalize(),
                f"{row['members_checked']:,}",
                f"{row['matches_found']:,}",
                f"{row['new_matches']:,}",
                format_duration_ms(row["duration_ms"]),
                row["error_message"] or "",
            ]
            return values[col]
        if role == Qt.ItemDataRole.ToolTipRole and col == 6:
            return row["error_message"] or None
        return None

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self._rows = [dict(r) for r in rows]
        self.endResetModel()


class ActivityPage(QWidget):
    def __init__(self, activity_model: ActivityModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)
        layout.addWidget(PageHeader("Activity", "The latest 100 events and recent scan history"))

        body = QHBoxLayout()
        body.setSpacing(14)

        feed_card = QFrame()
        feed_card.setObjectName("Card")
        feed_layout = QVBoxLayout(feed_card)
        feed_layout.setContentsMargins(18, 16, 14, 14)
        feed_layout.setSpacing(10)
        title = QLabel("Activity log")
        title.setObjectName("H2")
        feed_layout.addWidget(title)
        feed = QListView()
        feed.setObjectName("ActivityFeed")
        feed.setModel(activity_model)
        feed.setItemDelegate(ActivityDelegate(feed, compact=False))
        feed.setUniformItemSizes(True)
        feed.setSelectionMode(QListView.SelectionMode.NoSelection)
        feed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        feed.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        feed_layout.addWidget(feed, 1)
        body.addWidget(feed_card, 5)

        history_card = QFrame()
        history_card.setObjectName("Card")
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(18, 16, 14, 14)
        history_layout.setSpacing(10)
        history_title = QLabel("Scan history")
        history_title.setObjectName("H2")
        history_layout.addWidget(history_title)
        self.history_model = ScanHistoryModel(self)
        table = QTableView()
        table.setObjectName("HistoryTable")
        table.setModel(self.history_model)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for col in range(6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        history_layout.addWidget(table, 1)
        body.addWidget(history_card, 6)

        layout.addLayout(body, 1)
