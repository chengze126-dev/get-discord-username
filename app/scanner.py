"""Periodic member scanner.

Lifecycle (default interval 60 s, never less):

1. Wait until the Discord connection reports the configured guild as ready.
2. Make sure the member cache is complete (gateway chunking / REST paging).
3. Walk every member, skipping bots if configured, and keep those whose
   ``joined_at`` is strictly before the cutoff (both are aware UTC datetimes).
4. For each match, compute the channels the member can view using Discord's
   permission model (role + overwrite resolution) with a per-scan cache keyed
   by the member's permission-relevant signature.
5. Upsert matches into SQLite keyed by (guild, Discord user ID) - existing rows
   are refreshed, never duplicated - and record the scan in scan_history.
6. Emit signals so the UI can update incrementally and notifications can fire.

The next automatic scan is scheduled ``interval`` seconds after the previous
scan *started*; "Scan Now" runs immediately and restarts the countdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import discord
from PySide6.QtCore import QObject, Signal

from app.config import MIN_SCAN_INTERVAL, Settings
from app.database import Database, DatabaseError
from app.discord_service import DiscordService, describe_exception
from app.models import ChannelInfo, ErrorKind, MemberSnapshot, ScanResult
from app.utils import ensure_utc, plural, utcnow

log = logging.getLogger(__name__)

MANUAL_SCAN_COOLDOWN = 5.0
YIELD_EVERY = 250


def _channel_kind(channel: discord.abc.GuildChannel) -> str | None:
    if isinstance(channel, discord.CategoryChannel):
        return None
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.ForumChannel):
        return "media" if getattr(channel, "is_media", lambda: False)() else "forum"
    if isinstance(channel, discord.TextChannel):
        return "news" if channel.is_news() else "text"
    return None


class ChannelAccessResolver:
    """Computes which guild channels a member can view, with caching.

    Channel permissions depend on the member's roles, member-specific
    overwrites, ownership and timeout state. Members sharing the same
    signature share the same result, so large guilds with a handful of role
    combinations need only a handful of permission computations per scan.
    """

    def __init__(self, guild: discord.Guild) -> None:
        self.guild = guild
        channels = [c for c in guild.channels if _channel_kind(c) is not None]
        channels.sort(key=lambda c: ((c.category.position if c.category else -1), c.position, c.id))
        self.channels = channels
        self._member_overwrite_ids: set[int] = set()
        for channel in channels:
            for target in channel.overwrites:
                if not isinstance(target, discord.Role):
                    self._member_overwrite_ids.add(target.id)
        self._cache: dict[tuple, list[ChannelInfo]] = {}

    def accessible_channels(self, member: discord.Member) -> list[ChannelInfo]:
        key = (
            tuple(sorted(role.id for role in member.roles)),
            member.id if member.id in self._member_overwrite_ids else None,
            member.id == self.guild.owner_id,
            member.is_timed_out(),
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result: list[ChannelInfo] = []
        for channel in self.channels:
            try:
                if channel.permissions_for(member).view_channel:
                    result.append(ChannelInfo(channel.id, channel.name, _channel_kind(channel) or "text"))
            except Exception as exc:  # noqa: BLE001 - one bad channel must not break the scan
                log.debug("Permission check failed for channel %s: %s", channel.id, exc)
        self._cache[key] = result
        return result


def _username(member: discord.Member) -> str:
    discriminator = getattr(member, "discriminator", "0")
    if discriminator and discriminator != "0":
        return f"{member.name}#{discriminator}"
    return member.name


def _avatar_url(member: discord.Member) -> str | None:
    try:
        return member.display_avatar.replace(size=128, format="png").url
    except Exception:  # noqa: BLE001
        return None


class Scanner(QObject):
    scan_started = Signal(bool)                  # manual?
    scan_finished = Signal(object)               # ScanResult
    schedule_changed = Signal(object)            # next scan monotonic time | None
    paused_changed = Signal(bool)
    activity = Signal(str, str)                  # event_type, message
    error_occurred = Signal(str, str)            # ErrorKind value, message

    def __init__(self, db: Database, discord_service: DiscordService, settings: Settings) -> None:
        super().__init__()
        self._db = db
        self._discord = discord_service
        self._settings = settings
        self._paused = False
        self._running = False
        self._scan_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._manual_requested = False
        self._task: asyncio.Task | None = None
        self._next_scan_at: float | None = None
        self._last_manual = 0.0
        self.last_result: ScanResult | None = None

    # ----------------------------------------------------------------- public

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def is_scanning(self) -> bool:
        return self._scan_lock.locked()

    @property
    def next_scan_at(self) -> float | None:
        return self._next_scan_at

    def seconds_until_next(self) -> float | None:
        if self._paused or self._next_scan_at is None:
            return None
        return max(0.0, self._next_scan_at - time.monotonic())

    def update_settings(self, settings: Settings) -> None:
        interval_changed = settings.scan_interval != self._settings.scan_interval
        self._settings = settings
        if interval_changed and self._next_scan_at is not None:
            self._set_next(time.monotonic() + settings.scan_interval)
            self._wake.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("Scanner task ended with an error")

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self.paused_changed.emit(True)
            self.activity.emit("info", "Monitoring paused")

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self.paused_changed.emit(False)
            self.activity.emit("info", "Monitoring resumed")
            if self._next_scan_at is None or self._next_scan_at < time.monotonic():
                self._set_next(time.monotonic())
            self._wake.set()

    def request_scan(self) -> bool:
        """Scan immediately. Returns False if throttled or already scanning."""
        now = time.monotonic()
        if self.is_scanning or now - self._last_manual < MANUAL_SCAN_COOLDOWN:
            return False
        self._last_manual = now
        self._manual_requested = True
        self._wake.set()
        return True

    # --------------------------------------------------------------- internal

    def _set_next(self, when: float | None) -> None:
        self._next_scan_at = when
        self.schedule_changed.emit(when)

    async def _sleep_until_wake(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _loop(self) -> None:
        self._set_next(time.monotonic() + 1.0)  # first scan shortly after the guild is ready
        while self._running:
            manual = self._manual_requested
            due = self._next_scan_at is not None and time.monotonic() >= self._next_scan_at

            if manual or (due and not self._paused):
                self._manual_requested = False
                if not self._discord.is_ready:
                    if manual:
                        self.error_occurred.emit(
                            ErrorKind.DISCONNECTED.value, "Cannot scan: Discord is not connected to the guild yet."
                        )
                    # Retry shortly once the connection is back; don't count as a scan.
                    self._set_next(time.monotonic() + 5.0)
                    await self._sleep_until_wake(5.0)
                    continue

                started = time.monotonic()
                self._set_next(started + self._settings.scan_interval)
                await self.run_scan(manual=manual)
                # Fixed cadence measured from the start of this scan, never below the minimum
                # interval. If the scan itself overran the interval, leave a short breather.
                next_at = started + max(MIN_SCAN_INTERVAL, self._settings.scan_interval)
                if next_at <= time.monotonic():
                    next_at = time.monotonic() + 5.0
                self._set_next(next_at)
                continue

            timeout = None
            if not self._paused and self._next_scan_at is not None:
                timeout = max(0.05, self._next_scan_at - time.monotonic())
            await self._sleep_until_wake(timeout)

    async def run_scan(self, manual: bool = False) -> ScanResult:
        async with self._scan_lock:
            self.scan_started.emit(manual)
            started_at = utcnow()
            t0 = time.perf_counter()
            guild = self._discord.guild
            settings = self._settings
            try:
                if guild is None:
                    raise RuntimeError("Guild is not available.")
                result = await self._scan_guild(guild, settings, started_at, t0, manual)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, DatabaseError):
                    kind, message = ErrorKind.DATABASE, f"Database error: {exc}"
                elif isinstance(exc, RuntimeError):
                    kind, message = ErrorKind.SCAN, str(exc)
                else:
                    kind, message = describe_exception(exc)
                log.exception("Scan failed")
                result = ScanResult(
                    started_at=started_at,
                    completed_at=utcnow(),
                    members_checked=0,
                    matches_found=0,
                    new_matches=0,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    status="error",
                    error_message=message,
                    manual=manual,
                )
                self.error_occurred.emit(kind.value, message)
                try:
                    self._db.add_scan(self._discord.guild_id, result)
                except DatabaseError:
                    pass

            self.last_result = result
            self.scan_finished.emit(result)
            return result

    async def _scan_guild(
        self, guild: discord.Guild, settings: Settings, started_at: datetime, t0: float, manual: bool
    ) -> ScanResult:
        cutoff = ensure_utc(settings.cutoff)
        members, complete = await self._discord.ensure_member_cache(guild)
        resolver = ChannelAccessResolver(guild)

        checked = 0
        skipped = 0
        matches: list[MemberSnapshot] = []
        for index, member in enumerate(members):
            if index and index % YIELD_EVERY == 0:
                await asyncio.sleep(0)  # keep the Qt event loop responsive
            try:
                if settings.ignore_bots and member.bot:
                    continue
                checked += 1
                joined_at = member.joined_at
                if joined_at is None:
                    continue  # Discord may omit joined_at (e.g. guest members)
                joined_at = ensure_utc(joined_at)
                if joined_at >= cutoff:
                    continue
                matches.append(
                    MemberSnapshot(
                        user_id=member.id,
                        guild_id=guild.id,
                        username=_username(member),
                        display_name=member.display_name,
                        avatar_url=_avatar_url(member),
                        joined_at=joined_at,
                        channels=resolver.accessible_channels(member),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never fail a whole scan for one member
                skipped += 1
                log.warning("Skipping member %s: %s", getattr(member, "id", "?"), exc)

        seen_at = utcnow()
        upsert = await asyncio.to_thread(self._db.upsert_members, guild.id, matches, seen_at)
        if complete:
            await asyncio.to_thread(self._db.mark_not_present, guild.id, {m.user_id for m in matches})

        new_records = self._db.get_members_by_ids(guild.id, upsert.new_user_ids)
        new_records.sort(key=lambda r: r.joined_at or seen_at)
        result = ScanResult(
            started_at=started_at,
            completed_at=utcnow(),
            members_checked=checked,
            matches_found=len(matches),
            new_matches=len(upsert.new_user_ids),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            status="success",
            new_members=new_records,
            manual=manual,
        )
        self._db.add_scan(guild.id, result)

        feed_limit = 20
        for record in new_records[:feed_limit]:
            self.activity.emit("found", f"Found {record.username}")
        if len(new_records) > feed_limit:
            self.activity.emit("found", f"Found {len(new_records) - feed_limit:,} more members")
        summary = f"Scan completed - {checked:,} checked, {plural(len(matches), 'match', 'es')}"
        if skipped:
            summary += f", {skipped} skipped"
        if not complete:
            summary += " (member list partially cached)"
        self.activity.emit("scan", summary)
        return result
