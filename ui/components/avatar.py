"""Avatar painting helpers (image with initials fallback)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from app.avatars import AvatarCache
from ui.theme import ThemeManager

_FALLBACK_COLORS = ["#6C72F5", "#3AA7F0", "#2FB380", "#E0A33A", "#E0645E", "#B06CF0", "#E2689F", "#47B3B0"]


def fallback_color(user_id: int) -> QColor:
    return QColor(_FALLBACK_COLORS[(user_id ^ (user_id >> 22)) % len(_FALLBACK_COLORS)])


def initials(name: str) -> str:
    cleaned = [c for c in name if c.isalnum()]
    return (cleaned[0] if cleaned else "?").upper()


def paint_avatar(
    painter: QPainter,
    rect: QRectF,
    cache: AvatarCache | None,
    url: str | None,
    user_id: int,
    name: str,
    dpr: float = 1.0,
) -> None:
    size = int(rect.width())
    pixmap = cache.rounded(url, size, dpr) if cache is not None else None
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if pixmap is not None:
        painter.drawPixmap(rect.toRect(), pixmap)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fallback_color(user_id))
        painter.drawEllipse(rect)
        font = QFont(painter.font())
        font.setPixelSize(max(9, int(size * 0.42)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials(name))
    painter.restore()


class AvatarWidget(QWidget):
    def __init__(self, cache: AvatarCache | None, size: int = 64, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._size = size
        self._url: str | None = None
        self._user_id = 0
        self._name = ""
        self.setFixedSize(size + 6, size + 6)
        if cache is not None:
            cache.avatar_ready.connect(self._on_ready)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._size + 6, self._size + 6)

    def set_member(self, url: str | None, user_id: int, name: str) -> None:
        self._url, self._user_id, self._name = url, user_id, name
        self.update()

    def _on_ready(self, url: str) -> None:
        if url == self._url:
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = ThemeManager.instance().palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QRectF(0.5, 0.5, self._size + 5, self._size + 5)
        painter.setPen(QColor(p.border_strong))
        painter.setBrush(QColor(p.surface))
        painter.drawEllipse(ring)
        paint_avatar(
            painter, QRectF(3, 3, self._size, self._size), self._cache, self._url, self._user_id, self._name,
            self.devicePixelRatioF(),
        )
        painter.end()
