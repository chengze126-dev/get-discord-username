"""Compact navigation sidebar."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ui.components.icons import logo_pixmap, themed_icon
from ui.components.status_indicator import ConnectionCard
from ui.theme import Palette, ThemeManager

NAV_ITEMS = [
    ("dashboard", "Dashboard", "dashboard"),
    ("members", "Members", "members"),
    ("activity", "Activity", "activity"),
    ("settings", "Settings", "settings"),
]


class Sidebar(QFrame):
    navigate = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(224)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(32))
        logo.setFixedSize(32, 32)
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("Member Scout")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("Discord Member Scout")
        subtitle.setObjectName("BrandSubtitle")
        names.addWidget(title)
        names.addWidget(subtitle)
        brand.addLayout(names, 1)
        layout.addLayout(brand)
        layout.addSpacing(22)

        section = QLabel("WORKSPACE")
        section.setObjectName("SidebarSection")
        layout.addWidget(section)
        layout.addSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, label, icon in NAV_ITEMS:
            button = QPushButton(f"  {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIconSize(QSize(17, 17))
            button.setProperty("icon_name", icon)
            button.clicked.connect(lambda _checked=False, k=key: self.navigate.emit(k))
            self._group.addButton(button)
            self._buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self.connection = ConnectionCard()
        layout.addWidget(self.connection)

        self._buttons["dashboard"].setChecked(True)
        ThemeManager.instance().changed.connect(self._apply_theme)
        self._group.buttonToggled.connect(lambda *_: self._apply_theme(ThemeManager.instance().palette))
        self._apply_theme(ThemeManager.instance().palette)

    def set_current(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def _apply_theme(self, p: Palette) -> None:
        for button in self._buttons.values():
            color = p.text if button.isChecked() else p.text_muted
            button.setIcon(themed_icon(button.property("icon_name"), color, 17))
