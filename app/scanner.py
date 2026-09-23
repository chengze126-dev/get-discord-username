"""Periodic, per-server member scanner.

Each cycle (default every 60 s, never less) syncs every configured server
*separately*, one after another:

1. Wait until the Discord connection is ready. Servers the bot is not in are
   skipped and reported, and never block the other servers.
2. Make sure that server's member cache is complete (gateway chunking / REST
   paging). A manual refresh re-requests the list, throttled per server.
3. Build a snapshot of every member of *that* guild object: username, display
   name, user ID, joined date, bot flag, roles and viewable channels.
4. Upsert the snapshots into SQLite under that server's guild_id, keyed by
   (guild_id, Discord user ID). Rows are refreshed, never duplicated, and
   members who left are flagged.
5. Count members that joined before the cutoff (both aware UTC datetimes).
   New ones feed the activity log and desktop notifications.

The next automatic cycle is scheduled ``interval`` seconds after the previous
cycle *started*. "Scan Now" / "Refresh all" run a full cycle immediately;
"Refresh server" syncs a single server without changing the schedule.
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
from app.models import (
    ChannelInfo,
    ErrorKind,
    MemberSnapshot,
    RoleInfo,
    ScanResult,
    channel_set_key,
    channels_to_json,
)
from app.utils import ensure_utc, plural, utcnow

log = logging.getLogger(__name__)

MANUAL_SCAN_COOLDOWN = 3.0
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
    """Computes which channels of ONE guild a member can view, with caching.

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
        self._cache: dict[tuple, tuple[list[ChannelInfo], str]] = {}
        self.channel_sets: dict[str, str] = {}  # set key -> channels JSON (stored once per distinct set)

    def accessible_channels(self, member: discord.Member) -> tuple[list[ChannelInfo], str]:
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
        set_key = channel_set_key(result)
        self.channel_sets.setdefault(set_key, channels_to_json(result))
        self._cache[key] = (result, set_key)
        return result, set_key


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


def _roles(member: discord.Member) -> list[RoleInfo]:
    roles = [r for r in member.roles if not r.is_default()]
    roles.sort(key=lambda r: r.position, reverse=True)
    return [RoleInfo(r.id, r.name, r.color.value, r.position) for r in roles]


class Scanner(QObject):
    cycle_started = Signal(bool)                 # manual?
    guild_scan_started = Signal(object)             # guild id
    guild_progress = Signal(object, int, int)       # guild id, loaded, total
    guild_scan_finished = Signal(object)         # ScanResult (one server)
    cycle_finished = Signal(object)              # list[ScanResult]
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
        self._requested: set[int] = set()
        self._requested_all = False
        self._task: asyncio.Task | None = None
        self._next_scan_at: float | None = None
        self._last_manual: dict[object, float] = {}
        self._scanning_guild: int | None = None
        self.last_results: dict[int, ScanResult] = {}

    # ----------------------------------------------------------------- public

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def is_scanning(self) -> bool:
        return self._scan_lock.locked()

    @property
    def scanning_guild(self) -> int | None:
        return self._scanning_guild

    def seconds_until_next(self) -> float | None:
        if self._paused or self._next_scan_at is None:
            return None
        return max(0.0, self._next_scan_at - time.monotonic())

    def update_settings(self, settings: Settings) -> None:
        interval_changed = settings.scan_interval != self._settings.scan_interval
        self._settings = settings
        self.last_results = {g: r for g, r in self.last_results.items() if g in settings.guild_ids}
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

    def request_scan(self, guild_ids: list[int] | None = None) -> bool:
        """Refresh now: every server (``None``) or only the given ones.

        Returns False if the same request was made a moment ago.
        """
        key: object = "all" if guild_ids is None else tuple(sorted(guild_ids))
        now = time.monotonic()
        if now - self._last_manual.get(key, 0.0) < MANUAL_SCAN_COOLDOWN:
            return False
        self._last_manual[key] = now
        if guild_ids is None:
            self._requested_all = True
        else:
            self._requested.update(guild_ids)
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
        self._set_next(time.monotonic() + 1.0)  # first cycle shortly after connecting
        while self._running:
            manual = self._requested_all or bool(self._requested)
            due = self._next_scan_at is not None and time.monotonic() >= self._next_scan_at

            if manual or (due and not self._paused):
                configured = list(self._settings.guild_ids)
                full_cycle = not manual or self._requested_all
                if full_cycle:
                    guild_ids = configured
                else:
                    guild_ids = [g for g in configured if g in self._requested]
                self._requested.clear()
                self._requested_all = False

                if not self._discord.is_ready:
                    if manual:
                        self.error_occurred.emit(
                            ErrorKind.DISCONNECTED.value, "Cannot refresh: Discord is not connected yet."
                        )
                    if full_cycle:
                        self._set_next(time.monotonic() + 5.0)
                    await self._sleep_until_wake(5.0)
                    continue

                started = time.monotonic()
                if full_cycle:
                    self._set_next(started + self._settings.scan_interval)
                await self.run_cycle(guild_ids, manual=manual, force=manual)
                if full_cycle:
                    # Fixed cadence measured from the start of this cycle, never below the
                    # minimum interval. If the cycle overran the interval, leave a short breather.
                    next_at = started + max(MIN_SCAN_INTERVAL, self._settings.scan_interval)
                    if next_at <= time.monotonic():
                        next_at = time.monotonic() + 5.0
                    self._set_next(next_at)
                continue

            timeout = None
            if not self._paused and self._next_scan_at is not None:
                timeout = max(0.05, self._next_scan_at - time.monotonic())
            await self._sleep_until_wake(timeout)

    async def run_cycle(self, guild_ids: list[int], manual: bool = False, force: bool = False) -> list[ScanResult]:
        async with self._scan_lock:
            self.cycle_started.emit(manual)
            results: list[ScanResult] = []
            for guild_id in guild_ids:
                if guild_id not in self._settings.guild_ids:
                    continue  # removed from settings while queued
                results.append(await self.scan_guild(guild_id, manual=manual, force=force))
            self.cycle_finished.emit(results)
            return results

    async def scan_guild(self, guild_id: int, manual: bool = False, force: bool = False) -> ScanResult:
        """Sync one server. Never touches rows of any other guild."""
        started_at = utcnow()
        t0 = time.perf_counter()
        guild = self._discord.guild(guild_id)
        settings = self._settings
        if guild is None:
            result = ScanResult(
                guild_id=guild_id, guild_name="", started_at=started_at, completed_at=utcnow(),
                members_checked=0, members_loaded=0, matches_found=0, new_matches=0,
                duration_ms=0, status="skipped",
                error_message="The bot is not in this server or it is unavailable.",
                manual=manual,
            )
            self.guild_scan_finished.emit(result)
            return result

        self._scanning_guild = guild_id
        self.guild_scan_started.emit(guild_id)
        try:
            result = await self._scan(guild, settings, started_at, t0, manual, force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DatabaseError):
                kind, message = ErrorKind.DATABASE, f"Database error: {exc}"
            else:
                kind, message = describe_exception(exc)
            log.exception("Sync failed for guild %s", guild_id)
            result = ScanResult(
                guild_id=guild_id, guild_name=guild.name, started_at=started_at, completed_at=utcnow(),
                members_checked=0, members_loaded=0, matches_found=0, new_matches=0,
                duration_ms=int((time.perf_counter() - t0) * 1000), status="error",
                error_message=message, manual=manual,
            )
            self.error_occurred.emit(kind.value, f"{guild.name}: {message}")
            try:
                self._db.add_scan(result)
            except DatabaseError:
                pass
        finally:
            self._scanning_guild = None

        self.last_results[guild_id] = result
        self.guild_scan_finished.emit(result)
        return result

    async def _scan(
        self,
        guild: discord.Guild,
        settings: Settings,
        started_at: datetime,
        t0: float,
        manual: bool,
        force: bool,
    ) -> ScanResult:
        cutoff = ensure_utc(settings.cutoff)
        gid = guild.id

        def progress(loaded: int, total: int) -> None:
            self.guild_progress.emit(gid, loaded, total)

        members, complete = await self._discord.ensure_member_cache(guild, force=force, progress=progress)
        progress(len(members), guild.member_count or len(members))
        resolver = ChannelAccessResolver(guild)

        snapshots: list[MemberSnapshot] = []
        qualifying_ids: set[int] = set()
        checked = 0
        skipped = 0
        for index, member in enumerate(members):
            if index and index % YIELD_EVERY == 0:
                await asyncio.sleep(0)  # keep the Qt event loop responsive
            try:
                if member.guild.id != gid:
                    continue  # defensive: never mix members of different servers
                joined_at = ensure_utc(member.joined_at) if member.joined_at else None
                channels, set_key = resolver.accessible_channels(member)
                snapshots.append(
                    MemberSnapshot(
                        user_id=member.id,
                        guild_id=gid,
                        username=_username(member),
                        display_name=member.display_name,
                        avatar_url=_avatar_url(member),
                        joined_at=joined_at,
                        is_bot=member.bot,
                        roles=_roles(member),
                        channels=channels,
                        channel_key=set_key,
                    )
                )
                if settings.ignore_bots and member.bot:
                    continue
                checked += 1
                if joined_at is not None and joined_at < cutoff:
                    qualifying_ids.add(member.id)
            except Exception as exc:  # noqa: BLE001 - never fail a whole scan for one member
                skipped += 1
                log.warning("Skipping member %s of guild %s: %s", getattr(member, "id", "?"), gid, exc)

        seen_at = utcnow()
        self._db.upsert_guild(gid, guild.name, guild.member_count, None)
        upsert = await asyncio.to_thread(self._db.upsert_members, gid, snapshots, seen_at, resolver.channel_sets)
        if complete:
            await asyncio.to_thread(self._db.mark_not_present, gid, {s.user_id for s in snapshots})

        new_qualifying = [uid for uid in upsert.new_user_ids if uid in qualifying_ids]
        new_records = self._db.get_members_by_ids(gid, new_qualifying)
        new_records.sort(key=lambda r: r.joined_at or seen_at)
        result = ScanResult(
            guild_id=gid,
            guild_name=guild.name,
            started_at=started_at,
            completed_at=utcnow(),
            members_checked=checked,
            members_loaded=len(snapshots),
            matches_found=len(qualifying_ids),
            new_matches=len(new_qualifying),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            status="success",
            new_members=new_records,
            manual=manual,
        )
        self._db.add_scan(result)

        feed_limit = 20
        for record in new_records[:feed_limit]:
            self.activity.emit("found", f"Found {record.username} in {guild.name}")
        if len(new_records) > feed_limit:
            self.activity.emit("found", f"Found {len(new_records) - feed_limit:,} more members in {guild.name}")
        summary = (
            f"{guild.name}: synced {len(snapshots):,} members, "
            f"{plural(len(qualifying_ids), 'match', 'es')} before cutoff"
        )
        if skipped:
            summary += f", {skipped} skipped"
        if not complete:
            summary += " (member list partially cached)"
        self.activity.emit("scan", summary)
        return result
