"""In-app toast notifications stacked in the bottom-right corner."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.components.icons import icon_pixmap, themed_icon
from ui.theme import ThemeManager

MAX_TOASTS = 4
TOAST_WIDTH = 360

_KIND_ICON = {"success": "check", "error": "alert", "warning": "alert", "info": "info"}
_KIND_TOKEN = {"success": "success", "error": "danger", "warning": "warning", "info": "accent"}


class Toast(QFrame):
    def __init__(self, kind: str, title: str, body: str, duration_ms: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFixedWidth(TOAST_WIDTH)
        p = ThemeManager.instance().palette
        accent = getattr(p, _KIND_TOKEN.get(kind, "accent"))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 10, 12)
        layout.setSpacing(12)

        icon = QLabel()
        icon.setFixedSize(28, 28)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        soft = QColor(accent)
        icon.setStyleSheet(f"background: rgba({soft.red()},{soft.green()},{soft.blue()},0.16); border-radius: 8px;")
        icon.setPixmap(icon_pixmap(_KIND_ICON.get(kind, "info"), accent, 15))
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("ToastTitle")
        title_label.setWordWrap(True)
        text.addWidget(title_label)
        if body:
            body_label = QLabel(body)
            body_label.setObjectName("ToastBody")
            body_label.setWordWrap(True)
            text.addWidget(body_label)
        layout.addLayout(text, 1)

        close = QPushButton()
        close.setObjectName("IconButton")
        close.setIcon(themed_icon("close", p.text_faint, 14))
        close.setFixedSize(24, 24)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.dismiss)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

        self.setStyleSheet(f"QFrame#Toast {{ border-left: 3px solid {accent}; }}")

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._closing = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timer.start(duration_ms)

    def show_animated(self) -> None:
        self.show()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def enterEvent(self, event) -> None:  # noqa: N802 - pause auto-dismiss while hovered
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._closing:
            self._timer.start(2500)
        super().leaveEvent(event)

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self) -> None:
        manager = self.parent()
        self.hide()
        self.deleteLater()
        if isinstance(manager, QWidget):
            owner = getattr(manager, "_toast_manager", None)
            if owner is not None:
                owner.remove(self)


class ToastManager(QObject):
    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self._toasts: list[Toast] = []
        host._toast_manager = self  # type: ignore[attr-defined]
        host.installEventFilter(self)

    def show(self, kind: str, title: str, body: str = "", duration_ms: int = 5000) -> None:
        # Collapse identical consecutive toasts (e.g. repeated network errors).
        for toast in self._toasts:
            if toast.property("key") == (kind, title, body) and not toast._closing:
                toast._timer.start(duration_ms)
                return
        toast = Toast(kind, title, body, duration_ms, self._host)
        toast.setProperty("key", (kind, title, body))
        self._toasts.append(toast)
        while len(self._toasts) > MAX_TOASTS:
            self._toasts[0].dismiss()
            self._toasts.pop(0)
        toast.adjustSize()
        self._layout()
        toast.raise_()
        toast.show_animated()

    def remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._layout()

    def _layout(self) -> None:
        margin = 20
        y = self._host.height() - margin
        for toast in reversed(self._toasts):
            toast.adjustSize()
            y -= toast.height()
            toast.move(self._host.width() - TOAST_WIDTH - margin, y)
            toast.raise_()
            y -= 10

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._host and event.type() == QEvent.Type.Resize:
            self._layout()
        return False
