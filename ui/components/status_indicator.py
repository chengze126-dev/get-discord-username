"""Connection status dot (pulsing while connecting) and sidebar status card."""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.models import ConnectionState
from ui.theme import Palette, ThemeManager

_STATE_TOKEN = {
    ConnectionState.CONNECTED: "success",
    ConnectionState.CONNECTING: "warning",
    ConnectionState.DISCONNECTED: "text_faint",
    ConnectionState.ERROR: "danger",
}
STATE_LABEL = {
    ConnectionState.CONNECTED: "Discord Connected",
    ConnectionState.CONNECTING: "Connecting…",
    ConnectionState.DISCONNECTED: "Discord Disconnected",
    ConnectionState.ERROR: "Error",
}


class StatusDot(QWidget):
    def __init__(self, size: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._state = ConnectionState.DISCONNECTED
        self._pulse = 0.0
        self.setFixedSize(size + 8, size + 8)
        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(1400)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setLoopCount(-1)
        ThemeManager.instance().changed.connect(lambda _p: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._size + 8, self._size + 8)

    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, value: float) -> None:
        self._pulse = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def set_state(self, state: ConnectionState) -> None:
        self._state = state
        if state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED):
            if self._anim.state() != QPropertyAnimation.State.Running:
                self._anim.start()
            self._anim.setDuration(900 if state == ConnectionState.CONNECTING else 2400)
        else:
            self._anim.stop()
            self._pulse = 0.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = ThemeManager.instance().palette
        color = QColor(getattr(p, _STATE_TOKEN[self._state]))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        if self._pulse > 0:
            halo = QColor(color)
            halo.setAlphaF(0.35 * (1.0 - self._pulse))
            radius = self._size / 2 + 4 * self._pulse
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(center.x() - radius + 0.5, center.y() - radius + 0.5, radius * 2, radius * 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        r = self._size / 2
        painter.drawEllipse(QRectF(center.x() - r + 0.5, center.y() - r + 0.5, self._size, self._size))
        painter.end()


class ConnectionCard(QFrame):
    """Compact connection summary shown at the bottom of the sidebar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConnectionCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.dot = StatusDot(8)
        self.title = QLabel(STATE_LABEL[ConnectionState.DISCONNECTED])
        self.title.setObjectName("ConnTitle")
        row.addWidget(self.dot)
        row.addWidget(self.title, 1)
        layout.addLayout(row)

        self.guild = QLabel("Guild: —")
        self.guild.setObjectName("ConnDetail")
        self.members = QLabel("")
        self.members.setObjectName("ConnDetail")
        self.detail = QLabel("")
        self.detail.setObjectName("ConnDetail")
        self.detail.setWordWrap(True)
        for label in (self.guild, self.members, self.detail):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            layout.addWidget(label)
        self.detail.hide()
        ThemeManager.instance().changed.connect(self._apply_theme)

    def _apply_theme(self, _p: Palette) -> None:
        self.update()

    def set_state(self, state: ConnectionState, detail: str) -> None:
        self.dot.set_state(state)
        self.title.setText(STATE_LABEL[state])
        show_detail = state != ConnectionState.CONNECTED and bool(detail)
        metrics = self.detail.fontMetrics()
        self.detail.setText(metrics.elidedText(detail, Qt.TextElideMode.ElideRight, 360))
        self.detail.setToolTip(detail)
        self.detail.setVisible(show_detail)

    def set_guild(self, name: str | None, member_count: int | None) -> None:
        if name:
            elided = self.guild.fontMetrics().elidedText(f"Guild: {name}", Qt.TextElideMode.ElideRight, 170)
            self.guild.setText(elided)
            self.guild.setToolTip(name)
            self.members.setText(f"{member_count:,} members" if member_count is not None else "")
            self.members.setVisible(member_count is not None)
        else:
            self.guild.setText("Guild: —")
            self.members.setText("")
            self.members.hide()
