"""Plain data objects shared between the service layer and the UI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.utils import from_iso


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class ErrorKind(str, Enum):
    INVALID_TOKEN = "invalid_token"
    MISSING_TOKEN = "missing_token"
    MISSING_INTENT = "missing_intent"
    GUILD_NOT_FOUND = "guild_not_found"
    MISSING_PERMISSIONS = "missing_permissions"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    DISCONNECTED = "disconnected"
    DATABASE = "database"
    NOTIFICATION = "notification"
    SCAN = "scan"
    UNKNOWN = "unknown"


ERROR_TITLES: dict[ErrorKind, str] = {
    ErrorKind.INVALID_TOKEN: "Invalid Discord token",
    ErrorKind.MISSING_TOKEN: "Bot token required",
    ErrorKind.MISSING_INTENT: "Missing Server Members intent",
    ErrorKind.GUILD_NOT_FOUND: "Guild not found",
    ErrorKind.MISSING_PERMISSIONS: "Missing permissions",
    ErrorKind.NETWORK: "Network unavailable",
    ErrorKind.RATE_LIMITED: "Discord rate limited",
    ErrorKind.DISCONNECTED: "Discord disconnected",
    ErrorKind.DATABASE: "Database error",
    ErrorKind.NOTIFICATION: "Notification failure",
    ErrorKind.SCAN: "Scan failed",
    ErrorKind.UNKNOWN: "Unexpected error",
}


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    id: int
    name: str
    kind: str  # text | voice | stage | forum | news | media

    @property
    def label(self) -> str:
        return f"#{self.name}" if self.kind in {"text", "news", "forum", "media"} else self.name

    def to_dict(self) -> dict:
        return {"id": str(self.id), "name": self.name, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelInfo":
        return cls(id=int(data.get("id", 0)), name=str(data.get("name", "")), kind=str(data.get("kind", "text")))


@dataclass(slots=True)
class MemberSnapshot:
    """A qualifying member as observed during one scan."""

    user_id: int
    guild_id: int
    username: str
    display_name: str
    avatar_url: str | None
    joined_at: datetime
    channels: list[ChannelInfo]

    def channels_json(self) -> str:
        return json.dumps([c.to_dict() for c in self.channels], ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class MemberRecord:
    """A stored member row, as displayed in the UI."""

    id: int
    user_id: int
    guild_id: int
    username: str
    display_name: str
    avatar_url: str | None
    joined_at: datetime | None
    channel_count: int
    channels: list[ChannelInfo]
    first_detected_at: datetime | None
    last_seen_at: datetime | None
    notification_sent: bool
    in_guild: bool

    @classmethod
    def from_row(cls, row) -> "MemberRecord":
        try:
            raw_channels = json.loads(row["accessible_channels_json"] or "[]")
            channels = [ChannelInfo.from_dict(c) for c in raw_channels if isinstance(c, dict)]
        except (ValueError, TypeError):
            channels = []
        return cls(
            id=row["id"],
            user_id=int(row["discord_user_id"]),
            guild_id=int(row["guild_id"]),
            username=row["username"],
            display_name=row["display_name"] or row["username"],
            avatar_url=row["avatar_url"],
            joined_at=from_iso(row["joined_at"]),
            channel_count=int(row["accessible_channel_count"] or 0),
            channels=channels,
            first_detected_at=from_iso(row["first_detected_at"]),
            last_seen_at=from_iso(row["last_seen_at"]),
            notification_sent=bool(row["notification_sent"]),
            in_guild=bool(row["in_guild"]),
        )

    @property
    def shows_display_name(self) -> bool:
        return bool(self.display_name) and self.display_name != self.username


@dataclass(slots=True)
class UpsertResult:
    new_user_ids: list[int] = field(default_factory=list)
    updated: int = 0


@dataclass(slots=True)
class ScanResult:
    started_at: datetime
    completed_at: datetime
    members_checked: int
    matches_found: int
    new_matches: int
    duration_ms: int
    status: str  # success | error | skipped
    error_message: str | None = None
    new_members: list[MemberRecord] = field(default_factory=list)
    manual: bool = False


@dataclass(slots=True)
class ActivityEvent:
    id: int
    timestamp: datetime
    event_type: str  # found | scan | error | info | connection
    message: str
