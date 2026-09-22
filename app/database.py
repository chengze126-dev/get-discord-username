"""SQLite persistence layer with automatic schema migrations.

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

from app.models import ActivityEvent, MemberRecord, MemberSnapshot, ScanResult, UpsertResult
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
]

# Keys that must never be persisted in the settings table.
_FORBIDDEN_SETTING_KEYS = {"token", "bot_token", "discord_bot_token"}


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
                self._conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _execute(self, sql: str, params: Sequence | dict = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

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

    # ---------------------------------------------------------------- members

    def upsert_members(self, guild_id: int, snapshots: Iterable[MemberSnapshot], seen_at: datetime) -> UpsertResult:
        """Insert new qualifying members, refresh existing ones. Never duplicates rows."""
        snapshots = list(snapshots)
        seen_iso = to_iso(seen_at)
        gid = str(guild_id)
        result = UpsertResult()
        try:
            with self._lock, self._conn:
                existing = {
                    row[0]
                    for row in self._conn.execute(
                        "SELECT discord_user_id FROM members WHERE guild_id = ?", (gid,)
                    )
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
                            to_iso(snap.joined_at),
                            len(snap.channels),
                            snap.channels_json(),
                            seen_iso,
                            seen_iso,
                        )
                    )
                self._conn.executemany(
                    """
                    INSERT INTO members (
                        discord_user_id, guild_id, username, display_name, avatar_url, joined_at,
                        accessible_channel_count, accessible_channels_json,
                        first_detected_at, last_seen_at, notification_sent, in_guild
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
                    ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET
                        username                 = excluded.username,
                        display_name             = excluded.display_name,
                        avatar_url               = excluded.avatar_url,
                        joined_at                = excluded.joined_at,
                        accessible_channel_count = excluded.accessible_channel_count,
                        accessible_channels_json = excluded.accessible_channels_json,
                        last_seen_at             = excluded.last_seen_at,
                        in_guild                 = 1
                    """,
                    rows,
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not save members: {exc}") from exc
        return result

    def mark_not_present(self, guild_id: int, qualifying_ids: set[int]) -> int:
        """Flag stored members that are no longer present/qualifying in the guild."""
        gid = str(guild_id)
        try:
            with self._lock, self._conn:
                stored = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT discord_user_id FROM members WHERE guild_id = ? AND in_guild = 1", (gid,)
                    )
                ]
                gone = [(gid, uid) for uid in stored if int(uid) not in qualifying_ids]
                self._conn.executemany(
                    "UPDATE members SET in_guild = 0 WHERE guild_id = ? AND discord_user_id = ?", gone
                )
                return len(gone)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def load_members(self, guild_id: int, cutoff: datetime) -> list[MemberRecord]:
        rows = self._query(
            "SELECT * FROM members WHERE guild_id = ? AND joined_at < ? ORDER BY joined_at ASC",
            (str(guild_id), to_iso(cutoff)),
        )
        return [MemberRecord.from_row(row) for row in rows]

    def get_members_by_ids(self, guild_id: int, user_ids: Sequence[int]) -> list[MemberRecord]:
        if not user_ids:
            return []
        records: list[MemberRecord] = []
        ids = [str(uid) for uid in user_ids]
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._query(
                f"SELECT * FROM members WHERE guild_id = ? AND discord_user_id IN ({placeholders})",
                (str(guild_id), *chunk),
            )
            records.extend(MemberRecord.from_row(row) for row in rows)
        return records

    def pending_notifications(self, guild_id: int, cutoff: datetime) -> list[MemberRecord]:
        rows = self._query(
            "SELECT * FROM members WHERE guild_id = ? AND notification_sent = 0 AND joined_at < ? "
            "ORDER BY first_detected_at ASC, joined_at ASC",
            (str(guild_id), to_iso(cutoff)),
        )
        return [MemberRecord.from_row(row) for row in rows]

    def mark_notified(self, guild_id: int, user_ids: Sequence[int]) -> None:
        if not user_ids:
            return
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "UPDATE members SET notification_sent = 1 WHERE guild_id = ? AND discord_user_id = ?",
                    [(str(guild_id), str(uid)) for uid in user_ids],
                )
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

    def count_qualifying(self, guild_id: int, cutoff: datetime) -> int:
        row = self._query(
            "SELECT COUNT(*) FROM members WHERE guild_id = ? AND joined_at < ?",
            (str(guild_id), to_iso(cutoff)),
        )[0]
        return int(row[0])

    def count_discovered_since(self, guild_id: int, since: datetime, cutoff: datetime) -> int:
        row = self._query(
            "SELECT COUNT(*) FROM members WHERE guild_id = ? AND joined_at < ? AND first_detected_at >= ?",
            (str(guild_id), to_iso(cutoff), to_iso(since)),
        )[0]
        return int(row[0])

    # ------------------------------------------------------------ scan history

    def add_scan(self, guild_id: int | None, result: ScanResult) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO scan_history (guild_id, started_at, completed_at, members_checked,
                        matches_found, new_matches, duration_ms, status, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(guild_id) if guild_id else None,
                        to_iso(result.started_at),
                        to_iso(result.completed_at),
                        result.members_checked,
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
        return self._query("SELECT * FROM scan_history ORDER BY id DESC LIMIT ?", (limit,))

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
