"""Application settings and secure bot-token storage.

Non-secret settings live in the SQLite ``settings`` table. The bot token is
NEVER written to SQLite: it is read from the ``DISCORD_BOT_TOKEN`` environment
variable (optionally via a ``.env`` file) or stored in the operating system's
credential store through :mod:`keyring`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app import APP_ID
from app.utils import PROJECT_ROOT, SecretRedactingFilter, ensure_utc, from_iso, to_iso

if TYPE_CHECKING:
    from app.database import Database

log = logging.getLogger(__name__)

MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 24 * 60 * 60
DEFAULT_CUTOFF = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
THEMES = ("dark", "light", "system")

ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
GUILD_ENV_KEY = "DISCORD_GUILD_ID"

KEYRING_SERVICE = APP_ID
KEYRING_USERNAME = "discord-bot-token"


@dataclass(frozen=True)
class Settings:
    guild_id: int | None = None
    cutoff: datetime = DEFAULT_CUTOFF
    scan_interval: int = MIN_SCAN_INTERVAL
    ignore_bots: bool = True
    notifications_enabled: bool = True
    notification_sound: bool = True
    max_notifications_per_scan: int = 10
    theme: str = "dark"

    def with_changes(self, **changes) -> "Settings":
        return replace(self, **changes).normalized()

    def normalized(self) -> "Settings":
        theme = self.theme if self.theme in THEMES else "dark"
        interval = max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(self.scan_interval)))
        max_notes = max(1, min(1000, int(self.max_notifications_per_scan)))
        return replace(
            self,
            cutoff=ensure_utc(self.cutoff),
            scan_interval=interval,
            max_notifications_per_scan=max_notes,
            theme=theme,
        )


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value.isdigit():
        return None
    return int(value)


def write_env_value(key: str, value: str, path=ENV_PATH) -> None:
    """Set ``key=value`` in the .env file, keeping every other line unchanged.

    Creates the file from .env.example when it does not exist yet. Only
    non-secret values should be written here (the bot token never is).
    """
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif ENV_EXAMPLE_PATH.exists():
        lines = ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    new_line = f"{key}={value}"
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if not replaced and (stripped.startswith(f"{key}=") or stripped == key):
            lines[index] = new_line
            replaced = True
    if not replaced:
        lines.append(new_line)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.environ[key] = value  # keep this process consistent with the file


class SettingsStore:
    """Loads/saves :class:`Settings` from the database settings table."""

    def __init__(self, db: "Database") -> None:
        self._db = db

    def load(self) -> Settings:
        raw = self._db.get_all_settings()
        # .env / environment wins, so edits to .env (by hand or set_guild_id.py) take effect.
        guild_id = _parse_int(os.environ.get(GUILD_ENV_KEY)) or _parse_int(raw.get("guild_id"))
        cutoff = from_iso(raw.get("cutoff")) or DEFAULT_CUTOFF
        return Settings(
            guild_id=guild_id,
            cutoff=cutoff,
            scan_interval=_parse_int(raw.get("scan_interval")) or MIN_SCAN_INTERVAL,
            ignore_bots=_parse_bool(raw.get("ignore_bots"), True),
            notifications_enabled=_parse_bool(raw.get("notifications_enabled"), True),
            notification_sound=_parse_bool(raw.get("notification_sound"), True),
            max_notifications_per_scan=_parse_int(raw.get("max_notifications_per_scan")) or 10,
            theme=raw.get("theme") or "dark",
        ).normalized()

    def save(self, settings: Settings) -> None:
        settings = settings.normalized()
        self._db.set_settings(
            {
                "guild_id": str(settings.guild_id) if settings.guild_id else "",
                "cutoff": to_iso(settings.cutoff) or "",
                "scan_interval": str(settings.scan_interval),
                "ignore_bots": "1" if settings.ignore_bots else "0",
                "notifications_enabled": "1" if settings.notifications_enabled else "0",
                "notification_sound": "1" if settings.notification_sound else "0",
                "max_notifications_per_scan": str(settings.max_notifications_per_scan),
                "theme": settings.theme,
            }
        )


class TokenStore:
    """Resolves the bot token without ever persisting it in plaintext app storage.

    Resolution order:
      1. OS credential store (keyring) - set from the Settings page.
      2. ``DISCORD_BOT_TOKEN`` environment variable / ``.env`` file.
      3. Session-only in-memory token (used when no credential store is available).
    """

    def __init__(self) -> None:
        self._session_token: str | None = None
        self._keyring_module = None
        self._keyring_checked = False

    def _keyring(self):
        """Return the keyring module if a usable OS credential store exists (cached)."""
        if self._keyring_checked:
            return self._keyring_module
        self._keyring_checked = True
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            if not isinstance(backend, fail.Keyring):
                self._keyring_module = keyring
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - broken backends can even raise non-Exception panics
            log.warning("OS credential store unavailable: %s", type(exc).__name__)
        return self._keyring_module

    @property
    def keyring_available(self) -> bool:
        return self._keyring() is not None

    def get(self) -> str | None:
        token = self._get_keyring_token() or os.environ.get("DISCORD_BOT_TOKEN", "").strip() or self._session_token
        token = token or None
        SecretRedactingFilter.register(token)
        return token

    def source(self) -> str:
        if self._get_keyring_token():
            return "OS credential store"
        if os.environ.get("DISCORD_BOT_TOKEN", "").strip():
            return "DISCORD_BOT_TOKEN environment variable"
        if self._session_token:
            return "this session only (not persisted)"
        return "not configured"

    def _get_keyring_token(self) -> str | None:
        kr = self._keyring()
        if kr is None:
            return None
        try:
            return (kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME) or "").strip() or None
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001
            log.warning("Could not read token from credential store: %s", type(exc).__name__)
            return None

    def set(self, token: str) -> bool:
        """Store the token. Returns True if it was persisted to the OS credential store."""
        token = token.strip()
        SecretRedactingFilter.register(token)
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
                self._session_token = None
                return True
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001
                log.warning("Could not write token to credential store: %s", type(exc).__name__)
        self._session_token = token
        return False

    def clear(self) -> None:
        self._session_token = None
        kr = self._keyring()
        if kr is None:
            return
        try:
            kr.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - nothing stored / backend failure
            pass
