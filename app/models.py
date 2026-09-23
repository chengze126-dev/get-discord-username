"""Plain data objects shared between the service layer and the UI."""

from __future__ import annotations

import hashlib
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


class GuildStatus(str, Enum):
    PENDING = "pending"          # configured, not resolved yet (e.g. not connected)
    AVAILABLE = "available"      # bot is in the guild, waiting for first sync
    LOADING = "loading"          # member list is being fetched
    SYNCED = "synced"            # last sync succeeded
    ERROR = "error"              # last sync failed
    NOT_MEMBER = "not_member"    # bot is not in this guild / invalid ID
    UNAVAILABLE = "unavailable"  # Discord outage for this guild


GUILD_STATUS_LABEL = {
    GuildStatus.PENDING: "Waiting",
    GuildStatus.AVAILABLE: "Ready",
    GuildStatus.LOADING: "Loading…",
    GuildStatus.SYNCED: "Synced",
    GuildStatus.ERROR: "Error",
    GuildStatus.NOT_MEMBER: "Bot not in server",
    GuildStatus.UNAVAILABLE: "Unavailable",
}


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
    ErrorKind.GUILD_NOT_FOUND: "Server not found",
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


@dataclass(frozen=True, slots=True)
class RoleInfo:
    id: int
    name: str
    color: int  # 0xRRGGBB, 0 = default colour
    position: int

    @property
    def hex_color(self) -> str | None:
        return f"#{self.color:06X}" if self.color else None

    def to_dict(self) -> dict:
        return {"id": str(self.id), "name": self.name, "color": self.color, "position": self.position}

    @classmethod
    def from_dict(cls, data: dict) -> "RoleInfo":
        return cls(
            id=int(data.get("id", 0)),
            name=str(data.get("name", "")),
            color=int(data.get("color", 0) or 0),
            position=int(data.get("position", 0) or 0),
        )


def channels_to_json(channels: list[ChannelInfo]) -> str:
    return json.dumps([c.to_dict() for c in channels], ensure_ascii=False, separators=(",", ":"))


def channel_set_key(channels: list[ChannelInfo]) -> str:
    """Stable key for a set of channels, so members with identical access share one stored list."""
    digest = hashlib.sha1(",".join(str(c.id) for c in channels).encode()).hexdigest()
    return digest[:16]


def parse_channels(raw: str | None) -> list[ChannelInfo]:
    try:
        data = json.loads(raw or "[]")
        return [ChannelInfo.from_dict(c) for c in data if isinstance(c, dict)]
    except (ValueError, TypeError):
        return []


def parse_roles(raw: str | None) -> list[RoleInfo]:
    try:
        data = json.loads(raw or "[]")
        return [RoleInfo.from_dict(r) for r in data if isinstance(r, dict)]
    except (ValueError, TypeError):
        return []


@dataclass(slots=True)
class MemberSnapshot:
    """A guild member as observed during one sync."""

    user_id: int
    guild_id: int
    username: str
    display_name: str
    avatar_url: str | None
    joined_at: datetime | None
    is_bot: bool
    roles: list[RoleInfo]
    channels: list[ChannelInfo]
    channel_key: str

    def roles_json(self) -> str:
        return json.dumps([r.to_dict() for r in self.roles], ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class MemberRecord:
    """A stored member row, as displayed in the UI. Always tied to exactly one guild."""

    id: int
    user_id: int
    guild_id: int
    guild_name: str
    username: str
    display_name: str
    avatar_url: str | None
    joined_at: datetime | None
    is_bot: bool
    roles: list[RoleInfo]
    channel_count: int
    channels: list[ChannelInfo]
    first_detected_at: datetime | None
    last_seen_at: datetime | None
    notification_sent: bool
    in_guild: bool

    @classmethod
    def from_row(cls, row, channel_sets: dict[str, list[ChannelInfo]] | None = None) -> "MemberRecord":
        keys = row.keys()
        key = row["channel_set_key"] if "channel_set_key" in keys else None
        if key and channel_sets is not None and key in channel_sets:
            channels = channel_sets[key]
        else:
            channels = parse_channels(row["accessible_channels_json"])
        guild_name = (row["guild_name"] if "guild_name" in keys else "") or ""
        return cls(
            id=row["id"],
            user_id=int(row["discord_user_id"]),
            guild_id=int(row["guild_id"]),
            guild_name=guild_name,
            username=row["username"],
            display_name=row["display_name"] or row["username"],
            avatar_url=row["avatar_url"],
            joined_at=from_iso(row["joined_at"]),
            is_bot=bool(row["is_bot"]) if "is_bot" in keys else False,
            roles=parse_roles(row["roles_json"] if "roles_json" in keys else "[]"),
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

    @property
    def role_names(self) -> str:
        return ", ".join(r.name for r in self.roles)


@dataclass
class GuildState:
    """Live, per-server status shown on server cards and group headers."""

    guild_id: int
    name: str = ""
    member_count: int | None = None      # total reported by Discord
    loaded: int = 0                      # members stored and currently present
    qualifying: int = 0                  # present members that joined before the cutoff
    status: GuildStatus = GuildStatus.PENDING
    message: str = ""
    progress: float | None = None        # 0..1 while loading
    last_synced_at: datetime | None = None
    icon_url: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or f"Server {self.guild_id}"


@dataclass(slots=True)
class UpsertResult:
    new_user_ids: list[int] = field(default_factory=list)
    updated: int = 0


@dataclass(slots=True)
class ScanResult:
    guild_id: int | None
    guild_name: str
    started_at: datetime
    completed_at: datetime
    members_checked: int      # members evaluated (bots excluded when ignored)
    members_loaded: int       # all members synced into the directory
    matches_found: int        # members that joined before the cutoff
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
