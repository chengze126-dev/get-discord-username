"""SVG icon loading with runtime recolouring for the active theme."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from app.utils import ICONS_DIR, data_dir


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str:
    path = ICONS_DIR / f"{name}.svg"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"></svg>'


def _device_ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app else None
    return max(1.0, screen.devicePixelRatio() if screen else 1.0)


@lru_cache(maxsize=512)
def icon_pixmap(name: str, color: str, size: int = 18, dpr: float | None = None) -> QPixmap:
    ratio = dpr or _device_ratio()
    svg = _svg_source(name).replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    px = max(1, int(round(size * ratio)))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def themed_icon(name: str, color: str, size: int = 18) -> QIcon:
    icon = QIcon()
    icon.addPixmap(icon_pixmap(name, color, size))
    return icon


def themed_icon_file(name: str, color: str) -> str:
    """Write a recoloured SVG to the cache dir and return its path (for QSS url())."""
    cache = data_dir() / "icon-cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        path.write_text(_svg_source(name).replace("currentColor", color), encoding="utf-8")
    return str(path)


def logo_pixmap(size: int = 32) -> QPixmap:
    return icon_pixmap("logo", "#FFFFFF", size)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(icon_pixmap("logo", "#FFFFFF", size, 1.0))
    return icon


ICON_SIZE = QSize(16, 16)
