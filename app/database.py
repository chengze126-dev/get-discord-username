"""SQLite persistence layer with automatic schema migrations.

Every member row belongs to exactly one guild: the unique key is
``(guild_id, discord_user_id)`` and every member query is filtered by
``guild_id``. The same Discord user in two servers is stored as two
independent rows, so data from one server can never leak into another.

The connection is shared between the Qt/asyncio thread and short-lived worker
threads (``asyncio.to_thread``) that perform bulk writes, so every access is
serialised through a lock.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from app.models import ActivityEvent, ChannelInfo, MemberRecord, MemberSnapshot, ScanResult, UpsertResult, parse_channels
from app.utils import from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

ACTIVITY_LIMIT = 100
SCAN_HISTORY_LIMIT = 1000


class DatabaseError(RuntimeError):
    """Raised for any persistence failure, with a human-readable message."""


MIGRATIONS: list[str] = [
    # --- v1: initial schema -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS members (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_user_id           TEXT    NOT NULL,
        guild_id                  TEXT    NOT NULL,
        username                  TEXT    NOT NULL,
        display_name              TEXT,
        avatar_url                TEXT,
        joined_at                 TEXT    NOT NULL,
        accessible_channel_count  INTEGER NOT NULL DEFAULT 0,
        accessible_channels_json  TEXT    NOT NULL DEFAULT '[]',
        first_detected_at         TEXT    NOT NULL,
        last_seen_at              TEXT    NOT NULL,
        notification_sent         INTEGER NOT NULL DEFAULT 0,
        in_guild                  INTEGER NOT NULL DEFAULT 1,
        UNIQUE (guild_id, discord_user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_members_guild_joined ON members (guild_id, joined_at);
    CREATE INDEX IF NOT EXISTS idx_members_notification ON members (guild_id, notification_sent);

    CREATE TABLE IF NOT EXISTS scan_history (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id         TEXT,
        started_at       TEXT    NOT NULL,
        completed_at     TEXT,
        members_checked  INTEGER NOT NULL DEFAULT 0,
        matches_found    INTEGER NOT NULL DEFAULT 0,
        new_matches      INTEGER NOT NULL DEFAULT 0,
        duration_ms      INTEGER NOT NULL DEFAULT 0,
        status           TEXT    NOT NULL,
        error_message    TEXT
    );

    CREATE TABLE IF NOT EXISTS activity_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        message     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
    # --- v2: multi-server member directory ------------------------------------
    # Existing rows are preserved; new columns get safe defaults and are filled
    # in on the next sync of each server.
    """
    ALTER TABLE members ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE members ADD COLUMN roles_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE members ADD COLUMN channel_set_key TEXT;
    CREATE INDEX IF NOT EXISTS idx_members_guild_present ON members (guild_id, in_guild);

    CREATE TABLE IF NOT EXISTS guilds (
        guild_id        TEXT PRIMARY KEY,
        name            TEXT NOT NULL DEFAULT '',
        member_count    INTEGER,
        icon_url        TEXT,
        last_synced_at  TEXT,
        last_status     TEXT,
        last_error      TEXT
    );
    INSERT OR IGNORE INTO guilds (guild_id, name) SELECT DISTINCT guild_id, '' FROM members;

    -- Members with identical channel access share one stored channel list.
    CREATE TABLE IF NOT EXISTS channel_sets (
        guild_id       TEXT NOT NULL,
        set_key        TEXT NOT NULL,
        channels_json  TEXT NOT NULL,
        PRIMARY KEY (guild_id, set_key)
    );

    ALTER TABLE scan_history ADD COLUMN members_loaded INTEGER NOT NULL DEFAULT 0;
    """,
]

# Keys that must never be persisted in the settings table.
_FORBIDDEN_SETTING_KEYS = {"token", "bot_token", "discord_bot_token"}

_MEMBER_SELECT = (
    "SELECT m.*, COALESCE(g.name, '') AS guild_name FROM members m "
    "LEFT JOIN guilds g ON g.guild_id = m.guild_id "
)


def _placeholders(values: Sequence) -> str:
    return ",".join("?" for _ in values)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not open database at {path}: {exc}") from exc

    # ------------------------------------------------------------------ infra

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
                log.info("Applying database migration v%d", index)
                try:
                    self._conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _query(self, sql: str, params: Sequence | dict = ()) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    # --------------------------------------------------------------- settings

    def get_all_settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self._query("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, str]) -> None:
        bad = _FORBIDDEN_SETTING_KEYS.intersection(k.lower() for k in values)
        if bad:
            raise DatabaseError("Refusing to store secrets in the settings table.")
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    list(values.items()),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    # ----------------------------------------------------------------- guilds

    def upsert_guild(self, guild_id: int, name: str, member_count: int | None, icon_url: str | None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO guilds (guild_id, name, member_count, icon_url) VALUES (?, ?, ?, ?)
                    ON CONFLICT (guild_id) DO UPDATE SET
                        name = excluded.name,
                        member_count = COALESCE(excluded.member_count, guilds.member_count),
                        icon_url = COALESCE(excluded.icon_url, guilds.icon_url)
                    """,
                    (str(guild_id), name, member_count, icon_url),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def set_guild_sync_state(self, guild_id: int, status: str, error: str | None, synced_at: datetime | None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO guilds (guild_id, name) VALUES (?, '')", (str(guild_id),)
                )
                if synced_at is not None:
                    self._conn.execute(
                        "UPDATE guilds SET last_status = ?, last_error = ?, last_synced_at = ? WHERE guild_id = ?",
                        (status, error, to_iso(synced_at), str(guild_id)),
                    )
                else:
                    self._conn.execute(
                        "UPDATE guilds SET last_status = ?, last_error = ? WHERE guild_id = ?",
                        (status, error, str(guild_id)),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def get_guilds(self, guild_ids: Sequence[int]) -> dict[int, dict]:
        if not guild_ids:
            return {}
        ids = [str(g) for g in guild_ids]
        rows = self._query(f"SELECT * FROM guilds WHERE guild_id IN ({_placeholders(ids)})", ids)
        result = {}
        for row in rows:
            result[int(row["guild_id"])] = {
                "name": row["name"] or "",
                "member_count": row["member_count"],
                "icon_url": row["icon_url"],
                "last_synced_at": from_iso(row["last_synced_at"]),
                "last_status": row["last_status"],
                "last_error": row["last_error"],
            }
        return result

    # ---------------------------------------------------------------- members

    def upsert_members(
        self,
        guild_id: int,
        snapshots: Iterable[MemberSnapshot],
        seen_at: datetime,
        channel_sets: dict[str, str],
    ) -> UpsertResult:
        """Insert new members of ``guild_id`` and refresh existing ones. Never duplicates rows."""
        snapshots = list(snapshots)
        seen_iso = to_iso(seen_at)
        gid = str(guild_id)
        result = UpsertResult()
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT INTO channel_sets (guild_id, set_key, channels_json) VALUES (?, ?, ?) "
                    "ON CONFLICT (guild_id, set_key) DO UPDATE SET channels_json = excluded.channels_json",
                    [(gid, key, data) for key, data in channel_sets.items()],
                )
                existing = {
                    row[0]
                    for row in self._conn.execute("SELECT discord_user_id FROM members WHERE guild_id = ?", (gid,))
                }
                rows = []
                for snap in snapshots:
                    uid = str(snap.user_id)
                    if uid in existing:
                        result.updated += 1
                    else:
                        result.new_user_ids.append(snap.user_id)
                    rows.append(
                        (
                            uid,
                            gid,
                            snap.username,
                            snap.display_name,
                            snap.avatar_url,
                            to_iso(snap.joined_at) or "",
                            len(snap.channels),
                            snap.channel_key,
                            1 if snap.is_bot else 0,
                            snap.roles_json(),
                            seen_iso,
                            seen_iso,
                        )
                    )
                self._conn.executemany(
                    """
                    INSERT INTO members (
                        discord_user_id, guild_id, username, display_name, avatar_url, joined_at,
                        accessible_channel_count, accessible_channels_json, channel_set_key, is_bot, roles_json,
                        first_detected_at, last_seen_at, notification_sent, in_guild
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 0, 1)
                    ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET
                        username                 = excluded.username,
                        display_name             = excluded.display_name,
                        avatar_url               = excluded.avatar_url,
                        joined_at                = excluded.joined_at,
                        accessible_channel_count = excluded.accessible_channel_count,
                        accessible_channels_json = '[]',
                        channel_set_key          = excluded.channel_set_key,
                        is_bot                   = excluded.is_bot,
                        roles_json               = excluded.roles_json,
                        last_seen_at             = excluded.last_seen_at,
                        in_guild                 = 1
                    """,
                    rows,
                )
                self._conn.execute(
                    "DELETE FROM channel_sets WHERE guild_id = ? AND set_key NOT IN "
                    "(SELECT DISTINCT channel_set_key FROM members WHERE guild_id = ? AND channel_set_key IS NOT NULL)",
                    (gid, gid),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not save members: {exc}") from exc
        return result

    def mark_not_present(self, guild_id: int, present_ids: set[int]) -> int:
        """Flag stored members of ``guild_id`` that are no longer in that guild."""
        gid = str(guild_id)
        try:
            with self._lock, self._conn:
                stored = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT discord_user_id FROM members WHERE guild_id = ? AND in_guild = 1", (gid,)
                    )
                ]
                gone = [(gid, uid) for uid in stored if int(uid) not in present_ids]
                self._conn.executemany(
                    "UPDATE members SET in_guild = 0 WHERE guild_id = ? AND discord_user_id = ?", gone
                )
                return len(gone)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def _channel_sets(self, guild_id: int) -> dict[str, list[ChannelInfo]]:
        rows = self._query("SELECT set_key, channels_json FROM channel_sets WHERE guild_id = ?", (str(guild_id),))
        return {row["set_key"]: parse_channels(row["channels_json"]) for row in rows}

    @staticmethod
    def _qualifying_sql(cutoff: datetime | None, ignore_bots: bool) -> tuple[str, list]:
        clauses, params = [], []
        if cutoff is not None:
            clauses.append("m.joined_at <> '' AND m.joined_at < ?")
            params.append(to_iso(cutoff))
        if ignore_bots:
            clauses.append("m.is_bot = 0")
        return (" AND " + " AND ".join(clauses)) if clauses else "", params

    def load_members(
        self,
        guild_id: int,
        cutoff: datetime | None = None,
        ignore_bots: bool = False,
    ) -> list[MemberRecord]:
        """All stored members of one guild (optionally only those joined before ``cutoff``)."""
        extra, params = self._qualifying_sql(cutoff, ignore_bots)
        rows = self._query(
            _MEMBER_SELECT + f"WHERE m.guild_id = ?{extra} ORDER BY m.joined_at ASC",
            (str(guild_id), *params),
        )
        sets = self._channel_sets(guild_id)
        return [MemberRecord.from_row(row, sets) for row in rows]

    def get_members_by_ids(self, guild_id: int, user_ids: Sequence[int]) -> list[MemberRecord]:
        if not user_ids:
            return []
        sets = self._channel_sets(guild_id)
        records: list[MemberRecord] = []
        ids = [str(uid) for uid in user_ids]
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            rows = self._query(
                _MEMBER_SELECT + f"WHERE m.guild_id = ? AND m.discord_user_id IN ({_placeholders(chunk)})",
                (str(guild_id), *chunk),
            )
            records.extend(MemberRecord.from_row(row, sets) for row in rows)
        return records

    def pending_notifications(self, guild_ids: Sequence[int], cutoff: datetime, ignore_bots: bool) -> list[MemberRecord]:
        if not guild_ids:
            return []
        extra, params = self._qualifying_sql(cutoff, ignore_bots)
        ids = [str(g) for g in guild_ids]
        rows = self._query(
            _MEMBER_SELECT
            + f"WHERE m.guild_id IN ({_placeholders(ids)}) AND m.notification_sent = 0 AND m.in_guild = 1{extra} "
            "ORDER BY m.first_detected_at ASC, m.joined_at ASC",
            (*ids, *params),
        )
        return [MemberRecord.from_row(row) for row in rows]

    def mark_notified(self, records: Sequence[MemberRecord]) -> None:
        if not records:
            return
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "UPDATE members SET notification_sent = 1 WHERE guild_id = ? AND discord_user_id = ?",
                    [(str(r.guild_id), str(r.user_id)) for r in records],
                )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def count_members(self, guild_id: int, cutoff: datetime | None = None, ignore_bots: bool = False) -> int:
        """Members currently present in one guild (optionally only qualifying ones)."""
        extra, params = self._qualifying_sql(cutoff, ignore_bots)
        row = self._query(
            f"SELECT COUNT(*) FROM members m WHERE m.guild_id = ? AND m.in_guild = 1{extra}",
            (str(guild_id), *params),
        )[0]
        return int(row[0])

    def count_discovered_since(
        self, guild_ids: Sequence[int], since: datetime, cutoff: datetime, ignore_bots: bool
    ) -> int:
        if not guild_ids:
            return 0
        extra, params = self._qualifying_sql(cutoff, ignore_bots)
        ids = [str(g) for g in guild_ids]
        row = self._query(
            f"SELECT COUNT(*) FROM members m WHERE m.guild_id IN ({_placeholders(ids)}) "
            f"AND m.first_detected_at >= ?{extra}",
            (*ids, to_iso(since), *params),
        )[0]
        return int(row[0])

    # ------------------------------------------------------------ scan history

    def add_scan(self, result: ScanResult) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO scan_history (guild_id, started_at, completed_at, members_checked, members_loaded,
                        matches_found, new_matches, duration_ms, status, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(result.guild_id) if result.guild_id else None,
                        to_iso(result.started_at),
                        to_iso(result.completed_at),
                        result.members_checked,
                        result.members_loaded,
                        result.matches_found,
                        result.new_matches,
                        result.duration_ms,
                        result.status,
                        result.error_message,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM scan_history WHERE id NOT IN "
                    "(SELECT id FROM scan_history ORDER BY id DESC LIMIT ?)",
                    (SCAN_HISTORY_LIMIT,),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def recent_scans(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT s.*, COALESCE(g.name, '') AS guild_name FROM scan_history s "
            "LEFT JOIN guilds g ON g.guild_id = s.guild_id ORDER BY s.id DESC LIMIT ?",
            (limit,),
        )

    def last_successful_scan(self, guild_id: int) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM scan_history WHERE guild_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
            (str(guild_id),),
        )
        return rows[0] if rows else None

    # ---------------------------------------------------------------- activity

    def add_activity(self, event_type: str, message: str, timestamp: datetime | None = None) -> ActivityEvent:
        timestamp = timestamp or utcnow()
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO activity_log (timestamp, event_type, message) VALUES (?, ?, ?)",
                    (to_iso(timestamp), event_type, message),
                )
                self._conn.execute(
                    "DELETE FROM activity_log WHERE id NOT IN "
                    "(SELECT id FROM activity_log ORDER BY id DESC LIMIT ?)",
                    (ACTIVITY_LIMIT,),
                )
                return ActivityEvent(cursor.lastrowid or 0, timestamp, event_type, message)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def recent_activity(self, limit: int = ACTIVITY_LIMIT) -> list[ActivityEvent]:
        rows = self._query("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,))
        return [
            ActivityEvent(row["id"], from_iso(row["timestamp"]) or utcnow(), row["event_type"], row["message"])
            for row in rows
        ]
