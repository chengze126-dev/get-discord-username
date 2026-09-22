"""Dashboard statistic card."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.components.icons import icon_pixmap
from ui.theme import Palette, ThemeManager


class StatCard(QFrame):
    def __init__(self, title: str, icon: str, accent_token: str = "accent", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setMinimumHeight(118)
        self._icon_name = icon
        self._accent_token = accent_token

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._title = QLabel(title.upper())
        self._title.setObjectName("StatTitle")
        font = self._title.font()
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 0.8)
        self._title.setFont(font)
        self._icon = QLabel()
        self._icon.setFixedSize(30, 30)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self._title)
        top.addStretch(1)
        top.addWidget(self._icon)
        layout.addLayout(top)

        self._value = QLabel("—")
        self._value.setObjectName("StatValue")
        layout.addWidget(self._value)

        self._caption = QLabel("")
        self._caption.setObjectName("StatCaption")
        layout.addWidget(self._caption)
        layout.addStretch(1)

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(28)
        self._shadow.setOffset(0, 6)
        self.setGraphicsEffect(self._shadow)

        ThemeManager.instance().changed.connect(self._apply_theme)
        self._apply_theme(ThemeManager.instance().palette)

    def _apply_theme(self, p: Palette) -> None:
        accent = getattr(p, self._accent_token)
        soft = QColor(accent)
        soft.setAlphaF(0.14)
        self._icon.setPixmap(icon_pixmap(self._icon_name, accent, 16))
        self._icon.setStyleSheet(
            f"background: rgba({soft.red()},{soft.green()},{soft.blue()},{soft.alphaF():.2f}); border-radius: 9px;"
        )
        shadow = QColor(p.shadow)
        shadow.setAlphaF(0.35 if p.is_dark else 0.07)
        self._shadow.setColor(shadow)

    def set_value(self, value: str) -> None:
        if self._value.text() != value:
            self._value.setText(value)

    def set_caption(self, caption: str) -> None:
        if self._caption.text() != caption:
            self._caption.setText(caption)
