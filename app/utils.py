"""Shared helpers: paths, time handling, formatting and logging."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
ICONS_DIR = ASSETS_DIR / "icons"


def data_dir() -> Path:
    """Directory for the SQLite database, logs and avatar cache."""
    override = os.environ.get("SCOUT_DATA_DIR", "").strip()
    path = Path(override).expanduser() if override else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- time


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime. Naive values are interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def local_midnight_utc() -> datetime:
    """Start of the current local day, expressed in UTC."""
    local_now = datetime.now().astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def format_date(value: datetime | None) -> str:
    """Human friendly date, e.g. 'Mar 14, 2022'.

    Join dates are shown as UTC calendar dates so they are consistent with the
    UTC cutoff (a member who joined 2023-12-31 23:30 UTC never shows as 2024).
    """
    if value is None:
        return "Unknown"
    return ensure_utc(value).strftime("%b %d, %Y")


def format_long_date(value: datetime | None) -> str:
    """e.g. 'March 14, 2022' (UTC calendar date)."""
    if value is None:
        return "Unknown"
    utc = ensure_utc(value)
    return f"{utc.strftime('%B')} {utc.day}, {utc.year}"


def format_precise(value: datetime | None) -> str:
    """Precise timestamp with both local and UTC representations."""
    if value is None:
        return "Unknown"
    local = value.astimezone()
    utc = ensure_utc(value)
    return f"{local.strftime('%b %d, %Y %H:%M:%S')} local  ·  {utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def format_clock(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%H:%M:%S")


def format_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def format_int(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}"


def plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count:,} {word}{'' if count == 1 else suffix}"


# ------------------------------------------------------------------------ logging


class SecretRedactingFilter(logging.Filter):
    """Removes registered secrets and anything shaped like a bot token from logs."""

    # Discord bot tokens: base64(user id).timestamp.hmac
    _TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}")
    _secrets: set[str] = set()

    @classmethod
    def register(cls, secret: str | None) -> None:
        if secret and len(secret) >= 8:
            cls._secrets.add(secret)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return self._TOKEN_RE.sub("[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the app
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True


def setup_logging() -> Path:
    log_path = data_dir() / "scout.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redactor = SecretRedactingFilter()

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(redactor)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    return log_path
