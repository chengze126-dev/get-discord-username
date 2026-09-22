"""Asynchronous avatar loader with memory + disk caching.

Avatars are downloaded from Discord's CDN (URLs provided by the Bot API) on
the shared asyncio loop, so the GUI never blocks. Pixmaps are created on the
GUI thread once the bytes arrive.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import aiohttp
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap

from app.utils import data_dir

log = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = 6


class AvatarCache(QObject):
    avatar_ready = Signal(str)  # avatar url

    def __init__(self) -> None:
        super().__init__()
        self._dir = data_dir() / "avatars"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pixmaps: dict[str, QPixmap] = {}
        self._rounded: dict[tuple[str, int, float], QPixmap] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    def _path_for(self, url: str):
        return self._dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")

    def pixmap(self, url: str | None) -> QPixmap | None:
        if not url or url in self._failed:
            return None
        pix = self._pixmaps.get(url)
        if pix is not None:
            return pix
        path = self._path_for(url)
        if path.exists():
            pix = QPixmap()
            if pix.load(str(path)):
                self._pixmaps[url] = pix
                return pix
        if url not in self._pending and not self._closed:
            self._pending.add(url)
            asyncio.ensure_future(self._download(url))
        return None

    def rounded(self, url: str | None, size: int, dpr: float = 1.0) -> QPixmap | None:
        key = (url or "", size, dpr)
        cached = self._rounded.get(key)
        if cached is not None:
            return cached
        source = self.pixmap(url)
        if source is None:
            return None
        px = int(size * dpr)
        result = QPixmap(px, px)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addEllipse(0, 0, px, px)
        painter.setClipPath(path)
        scaled = source.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        result.setDevicePixelRatio(dpr)
        self._rounded[key] = result
        return result

    async def _download(self, url: str) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        try:
            async with self._semaphore:
                if self._closed:
                    return
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
                async with self._session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    data = await response.read()
            pix = QPixmap()
            if not pix.loadFromData(data):
                raise RuntimeError("unsupported image data")
            try:
                self._path_for(url).write_bytes(data)
            except OSError:
                pass
            self._pixmaps[url] = pix
            self.avatar_ready.emit(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - avatars are cosmetic
            log.debug("Avatar download failed: %s", exc)
            self._failed.add(url)
        finally:
            self._pending.discard(url)

    async def close(self) -> None:
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
