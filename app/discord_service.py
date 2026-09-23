"""Discord gateway connection management (official Bot API only).

One bot connection serves every configured server (guild). The service
reports the connection state and a separate status for each configured guild
(available, bot not a member, unavailable) through Qt signals. If the
connection cannot be (re-)established it reconnects with exponential backoff.

Only the ``guilds`` and ``members`` gateway intents are requested. ``members``
is the privileged *Server Members Intent* that must be enabled in the Discord
Developer Portal. Without it Discord refuses the connection (close code 4014)
and the service reports a clear error instead of retrying forever.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiohttp
import discord
from PySide6.QtCore import QObject, Signal

from app.models import ConnectionState, ErrorKind, GuildStatus

log = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 300.0
# Discord throttles REQUEST_GUILD_MEMBERS (gateway op 8). Once a guild is chunked
# the member cache is kept current by GUILD_MEMBER_ADD/UPDATE/REMOVE events, so
# re-chunking is only needed after a fresh session, and never more than this often.
CHUNK_COOLDOWN_SECONDS = 300.0
# A manual "Refresh" may re-request the member list, but not more often than this per guild.
FORCED_REFRESH_COOLDOWN = 60.0
RATE_LIMIT_NOTICE_COOLDOWN = 30.0

ProgressCallback = Callable[[int, int], None]  # (loaded, total)


@dataclass(frozen=True)
class GuildInfo:
    id: int
    name: str
    member_count: int
    cached_members: int
    icon_url: str | None = None


@dataclass
class ConnectionTestResult:
    ok: bool
    title: str
    details: list[str] = field(default_factory=list)
    error_kind: ErrorKind | None = None


@dataclass(frozen=True)
class BotGuild:
    id: int
    name: str
    approximate_members: int | None


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True   # guild, channel and role data (needed for permission checks)
    intents.members = True  # privileged: Server Members Intent
    return intents


def invite_url(application_id: int | str) -> str:
    """OAuth2 URL that adds the bot to a server (scope=bot, View Channels permission)."""
    return f"https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot&permissions=1024"


def describe_exception(exc: BaseException) -> tuple[ErrorKind, str]:
    """Map an exception to an error kind and human-readable explanation."""
    if isinstance(exc, discord.PrivilegedIntentsRequired):
        return (
            ErrorKind.MISSING_INTENT,
            "Discord rejected the connection because the Server Members Intent is disabled. "
            "Enable it in the Developer Portal → your application → Bot → Privileged Gateway Intents.",
        )
    if isinstance(exc, discord.LoginFailure):
        return (
            ErrorKind.INVALID_TOKEN,
            "Discord rejected the bot token. Reset the token in the Developer Portal and paste it in Settings.",
        )
    if isinstance(exc, discord.RateLimited):
        return (ErrorKind.RATE_LIMITED, f"Discord is rate limiting requests. Retry in {exc.retry_after:.0f}s.")
    if isinstance(exc, discord.Forbidden):
        return (
            ErrorKind.MISSING_PERMISSIONS,
            f"Discord denied access (403): {exc.text or 'missing permissions'}.",
        )
    if isinstance(exc, discord.NotFound):
        return (ErrorKind.GUILD_NOT_FOUND, f"Discord could not find the resource (404): {exc.text or ''}".strip())
    if isinstance(exc, discord.HTTPException):
        if exc.status == 429:
            return (ErrorKind.RATE_LIMITED, "Discord is rate limiting requests. The app will retry automatically.")
        if exc.status >= 500:
            return (ErrorKind.NETWORK, f"Discord is having server issues (HTTP {exc.status}). Retrying automatically.")
        return (ErrorKind.UNKNOWN, f"Discord API error (HTTP {exc.status}): {exc.text}")
    if isinstance(exc, discord.ConnectionClosed):
        return (ErrorKind.DISCONNECTED, f"The Discord gateway closed the connection (code {exc.code}).")
    if isinstance(exc, (aiohttp.ClientError, OSError, discord.GatewayNotFound, asyncio.TimeoutError)):
        return (
            ErrorKind.NETWORK,
            "Could not reach Discord. Check your internet connection, proxy or firewall.",
        )
    return (ErrorKind.UNKNOWN, f"{type(exc).__name__}: {exc}")


class _RateLimitLogHandler(logging.Handler):
    """discord.py handles 429s itself and logs a warning; surface that in the UI."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__(level=logging.WARNING)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "rate limit" in message.lower():
            self._callback(message)


def _guild_info(guild: discord.Guild) -> GuildInfo:
    icon = None
    try:
        if guild.icon is not None:
            icon = guild.icon.replace(size=128, format="png").url
    except Exception:  # noqa: BLE001
        icon = None
    return GuildInfo(
        id=guild.id,
        name=guild.name,
        member_count=guild.member_count or len(guild.members),
        cached_members=len(guild.members),
        icon_url=icon,
    )


class DiscordService(QObject):
    state_changed = Signal(str, str)             # ConnectionState value, detail text
    guild_status_changed = Signal(object, str, str, object)  # guild id, GuildStatus value, message, GuildInfo|None
    error_occurred = Signal(str, str)            # ErrorKind value, message
    ready_changed = Signal(bool)                 # True once the gateway session is ready

    def __init__(self, token_provider: Callable[[], str | None], guild_ids: tuple[int, ...]) -> None:
        super().__init__()
        self._token_provider = token_provider
        self._guild_ids: tuple[int, ...] = tuple(guild_ids)
        self._client: discord.Client | None = None
        self._runner_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._state = ConnectionState.DISCONNECTED
        self._ready = False
        self._last_chunk_at: dict[int, float] = {}
        self._chunk_locks: dict[int, asyncio.Lock] = {}
        self._last_rate_notice = 0.0
        self._reported_missing: set[int] = set()
        self._rate_handler = _RateLimitLogHandler(self._on_rate_limit_logged)
        logging.getLogger("discord.http").addHandler(self._rate_handler)
        logging.getLogger("discord.gateway").addHandler(self._rate_handler)

    # ----------------------------------------------------------------- public

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def guild_ids(self) -> tuple[int, ...]:
        return self._guild_ids

    @property
    def is_ready(self) -> bool:
        """True when the gateway session is ready (individual guilds may still be missing)."""
        return self._ready and self._client is not None and self._client.is_ready()

    def guild(self, guild_id: int) -> discord.Guild | None:
        """The live guild object, or None if the bot is not in it / it is unavailable."""
        if not self.is_ready:
            return None
        guild = self._client.get_guild(guild_id)  # type: ignore[union-attr]
        if guild is None or guild.unavailable:
            return None
        return guild

    def available_guild_ids(self) -> list[int]:
        return [gid for gid in self._guild_ids if self.guild(gid) is not None]

    def start(self) -> None:
        if self._runner_task is None or self._runner_task.done():
            self._stopping = False
            self._runner_task = asyncio.ensure_future(self._run_forever())

    def set_guild_ids(self, guild_ids: tuple[int, ...]) -> None:
        guild_ids = tuple(guild_ids)
        if guild_ids == self._guild_ids:
            return
        self._guild_ids = guild_ids
        self._reported_missing &= set(guild_ids)
        if self.is_ready:
            asyncio.ensure_future(self._resolve_guilds())
        else:
            self._update_connection_summary()

    async def restart(self) -> None:
        """Reconnect using the current token (e.g. after it was changed in Settings)."""
        client = self._client
        self._wake.set()
        if client is not None and not client.is_closed():
            await client.close()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        client = self._client
        if client is not None and not client.is_closed():
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.warning("Error while closing Discord client: %s", exc)
        if self._runner_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._runner_task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._runner_task.cancel()
            except Exception:  # noqa: BLE001
                pass
        logging.getLogger("discord.http").removeHandler(self._rate_handler)
        logging.getLogger("discord.gateway").removeHandler(self._rate_handler)
        self._set_state(ConnectionState.DISCONNECTED, "Stopped")

    async def ensure_member_cache(
        self,
        guild: discord.Guild,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> tuple[list[discord.Member], bool]:
        """Return this guild's members, fetching them if the cache is incomplete.

        Returns ``(members, complete)``. ``complete`` is True only when the list
        is known to contain every member (chunked via gateway or fully paged
        over REST). ``force`` re-requests the list (manual refresh), throttled
        per guild. Members are always read from *this* guild object only.
        """
        lock = self._chunk_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last_chunk_at.get(guild.id, 0.0)
            recently_forced = bool(last) and now - last < FORCED_REFRESH_COOLDOWN
            if guild.chunked and not (force and not recently_forced):
                return list(guild.members), True

            if not guild.chunked and last and (recently_forced or (now - last < CHUNK_COOLDOWN_SECONDS and not force)):
                # Recently chunked; the small drift is caused by joins/leaves in flight.
                members = list(guild.members)
                expected = guild.member_count or len(members)
                return members, len(members) >= expected * 0.99

            expected = guild.member_count or 0
            timeout = max(60.0, expected / 1000 * 3)
            log.info("Requesting full member list for guild %s (%s members)", guild.id, expected)
            chunk_task = asyncio.ensure_future(guild.chunk(cache=True))
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        await asyncio.wait_for(asyncio.shield(chunk_task), timeout=0.5)
                        break
                    except asyncio.TimeoutError:
                        if progress is not None:
                            progress(len(guild.members), expected)
                        if time.monotonic() > deadline:
                            raise
                self._last_chunk_at[guild.id] = time.monotonic()
                if progress is not None:
                    progress(len(guild.members), guild.member_count or len(guild.members))
                self._emit_guild(guild.id)
                return list(guild.members), True
            except asyncio.TimeoutError:
                chunk_task.cancel()
                log.warning("Gateway chunking timed out for guild %s; falling back to REST paging", guild.id)

            # REST fallback: GET /guilds/{id}/members, 1000 per page (discord.py paginates
            # and honours rate-limit headers automatically).
            members: list[discord.Member] = []
            async for member in guild.fetch_members(limit=None):
                members.append(member)
                if len(members) % 1000 == 0:
                    if progress is not None:
                        progress(len(members), expected)
                    await asyncio.sleep(0)
            self._last_chunk_at[guild.id] = time.monotonic()
            return members, True

    # --------------------------------------------------------------- internal

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        self.state_changed.emit(state.value, detail)

    def _set_ready(self, ready: bool) -> None:
        if ready != self._ready:
            self._ready = ready
            self.ready_changed.emit(ready)

    def _emit_error(self, kind: ErrorKind, message: str) -> None:
        log.error("%s: %s", kind.value, message)
        self.error_occurred.emit(kind.value, message)

    def _on_rate_limit_logged(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_rate_notice < RATE_LIMIT_NOTICE_COOLDOWN:
            return
        self._last_rate_notice = now
        self.error_occurred.emit(
            ErrorKind.RATE_LIMITED.value,
            "Discord is rate limiting requests. Requests are being delayed automatically.",
        )

    def _emit_guild(self, guild_id: int) -> None:
        """Emit the current status of one configured guild."""
        if guild_id not in self._guild_ids or self._client is None or not self._client.is_ready():
            return
        guild = self._client.get_guild(guild_id)
        if guild is None:
            self.guild_status_changed.emit(
                guild_id,
                GuildStatus.NOT_MEMBER.value,
                "The bot is not in this server, or the ID is wrong. Invite the bot with scope=bot.",
                None,
            )
        elif guild.unavailable:
            self.guild_status_changed.emit(
                guild_id, GuildStatus.UNAVAILABLE.value, "Temporarily unavailable (Discord outage)", None
            )
        else:
            self.guild_status_changed.emit(guild_id, GuildStatus.AVAILABLE.value, "", _guild_info(guild))

    def _update_connection_summary(self) -> None:
        if not self.is_ready:
            return
        if not self._guild_ids:
            self._set_state(ConnectionState.ERROR, "No servers configured")
            return
        available = len(self.available_guild_ids())
        total = len(self._guild_ids)
        self._set_state(ConnectionState.CONNECTED, f"{available} of {total} server{'s' if total != 1 else ''} available")

    def _make_client(self) -> discord.Client:
        client = discord.Client(
            intents=build_intents(),
            chunk_guilds_at_startup=False,  # only configured guilds are chunked, on demand
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.from_intents(build_intents()),
            max_ratelimit_timeout=60.0,
        )

        @client.event
        async def on_ready() -> None:
            log.info("Logged in as %s (id %s)", client.user, getattr(client.user, "id", "?"))
            self._set_ready(True)
            await self._resolve_guilds()

        @client.event
        async def on_resumed() -> None:
            self._set_ready(True)
            await self._resolve_guilds()

        @client.event
        async def on_disconnect() -> None:
            if self._stopping:
                return
            self._set_ready(False)
            self._set_state(ConnectionState.DISCONNECTED, "Connection lost — reconnecting…")

        @client.event
        async def on_guild_available(guild: discord.Guild) -> None:
            if guild.id in self._guild_ids:
                self._emit_guild(guild.id)
                self._update_connection_summary()

        @client.event
        async def on_guild_unavailable(guild: discord.Guild) -> None:
            if guild.id in self._guild_ids:
                self._emit_guild(guild.id)
                self._update_connection_summary()

        @client.event
        async def on_guild_join(guild: discord.Guild) -> None:
            if guild.id in self._guild_ids:
                self._reported_missing.discard(guild.id)
                self._emit_guild(guild.id)
                self._update_connection_summary()

        @client.event
        async def on_guild_remove(guild: discord.Guild) -> None:
            if guild.id in self._guild_ids:
                self.guild_status_changed.emit(
                    guild.id, GuildStatus.NOT_MEMBER.value, "The bot was removed from this server.", None
                )
                self._emit_error(
                    ErrorKind.GUILD_NOT_FOUND,
                    f"The bot was removed from '{guild.name}'. Re-invite it to keep monitoring that server.",
                )
                self._update_connection_summary()

        @client.event
        async def on_guild_update(_before: discord.Guild, after: discord.Guild) -> None:
            if after.id in self._guild_ids:
                self._emit_guild(after.id)

        @client.event
        async def on_member_join(member: discord.Member) -> None:
            if member.guild.id in self._guild_ids:
                self._emit_guild(member.guild.id)

        @client.event
        async def on_member_remove(member: discord.Member) -> None:
            if member.guild.id in self._guild_ids:
                self._emit_guild(member.guild.id)

        @client.event
        async def on_error(event_method: str, *args, **kwargs) -> None:
            log.exception("Unhandled error in Discord event %s", event_method)

        return client

    async def _resolve_guilds(self) -> None:
        client = self._client
        if client is None or not client.is_ready():
            return
        if not self._guild_ids:
            self._set_state(ConnectionState.ERROR, "No servers configured")
            self._emit_error(ErrorKind.GUILD_NOT_FOUND, "Add at least one Server (Guild) ID in Settings.")
            return
        missing: list[int] = []
        for guild_id in self._guild_ids:
            self._emit_guild(guild_id)
            if client.get_guild(guild_id) is None:
                missing.append(guild_id)
        new_missing = [gid for gid in missing if gid not in self._reported_missing]
        if new_missing:
            self._reported_missing.update(new_missing)
            ids = ", ".join(str(g) for g in new_missing)
            app_id = client.application_id or (client.user.id if client.user else "YOUR_CLIENT_ID")
            self._emit_error(
                ErrorKind.GUILD_NOT_FOUND,
                f"The bot is not a member of server {ids}. Invite it with: {invite_url(app_id)}",
            )
        self._update_connection_summary()

    async def _wait_for_wake(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _run_forever(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._stopping:
            token = self._token_provider()
            if not token:
                self._set_state(ConnectionState.ERROR, "Bot token not configured")
                self._emit_error(
                    ErrorKind.MISSING_TOKEN,
                    "Add your bot token in Settings or set the DISCORD_BOT_TOKEN environment variable.",
                )
                await self._wait_for_wake(None)
                continue

            self._wake.clear()
            self._set_state(ConnectionState.CONNECTING, "Connecting to Discord…")
            for gid in self._guild_ids:
                self.guild_status_changed.emit(gid, GuildStatus.PENDING.value, "Connecting…", None)
            client = self._make_client()
            self._client = client
            self._last_chunk_at.clear()
            wait_for_user = False
            connected_at = time.monotonic()
            try:
                await client.start(token, reconnect=True)
            except asyncio.CancelledError:
                raise
            except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as exc:
                kind, message = describe_exception(exc)
                short = "Missing Server Members intent" if kind == ErrorKind.MISSING_INTENT else "Invalid bot token"
                self._set_state(ConnectionState.ERROR, short)
                self._emit_error(kind, message)
                wait_for_user = True  # retrying cannot succeed until the user fixes the config
            except Exception as exc:  # noqa: BLE001 - network, HTTP, gateway errors
                kind, message = describe_exception(exc)
                state = ConnectionState.DISCONNECTED if kind == ErrorKind.NETWORK else ConnectionState.ERROR
                self._set_state(state, message)
                self._emit_error(kind, message)
            finally:
                self._set_ready(False)
                if not client.is_closed():
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001
                        pass

            if self._stopping:
                break
            if wait_for_user:
                await self._wait_for_wake(None)
                backoff = BACKOFF_INITIAL
                continue
            if self._wake.is_set():
                # Explicit restart requested (settings changed) - reconnect right away.
                self._wake.clear()
                backoff = BACKOFF_INITIAL
                continue

            if time.monotonic() - connected_at > 120:
                backoff = BACKOFF_INITIAL  # the previous session was healthy for a while
            delay = min(BACKOFF_MAX, backoff) * (0.8 + random.random() * 0.4)
            self._set_state(ConnectionState.DISCONNECTED, f"Reconnecting in {delay:.0f}s…")
            log.info("Reconnecting to Discord in %.1fs", delay)
            await self._wait_for_wake(delay)
            backoff = min(BACKOFF_MAX, backoff * 2)


async def _rest_client(token: str) -> discord.Client:
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    return client


@dataclass
class BotGuildList:
    guilds: list[BotGuild]
    invite_url: str      # adds THIS bot to a server (scope=bot)
    bot_name: str


async def list_bot_guilds(token: str) -> BotGuildList:
    """Servers the bot has been invited to (REST: GET /users/@me/guilds) plus its invite URL."""
    client = await _rest_client(token.strip())
    try:
        guilds = [
            BotGuild(g.id, g.name, g.approximate_member_count)
            async for g in client.fetch_guilds(limit=None, with_counts=True)
        ]
        guilds.sort(key=lambda g: g.name.casefold())
        app_info = await client.application_info()
        return BotGuildList(guilds, invite_url(app_info.id), str(client.user) if client.user else app_info.name)
    finally:
        await client.close()


async def test_connection(token: str, guild_ids: tuple[int, ...]) -> ConnectionTestResult:
    """Validate credentials and every configured server over REST (no gateway session)."""
    token = token.strip()
    if not token:
        return ConnectionTestResult(False, "Bot token required", ["Enter a bot token first."], ErrorKind.MISSING_TOKEN)

    client = discord.Client(intents=discord.Intents.none())
    details: list[str] = []
    try:
        await client.login(token)
        user = client.user
        details.append(f"Authenticated as bot {user} (ID {user.id if user else '?'})")

        app_info = await client.application_info()
        flags = app_info.flags
        if not (flags.gateway_guild_members or flags.gateway_guild_members_limited):
            return ConnectionTestResult(
                False,
                "Server Members Intent is disabled",
                details + ["Enable 'Server Members Intent' in the Developer Portal → Bot → Privileged Gateway Intents."],
                ErrorKind.MISSING_INTENT,
            )
        details.append("Server Members Intent is enabled")

        if not guild_ids:
            return ConnectionTestResult(
                False, "Server ID required", details + ["Enter at least one Server (Guild) ID."], ErrorKind.GUILD_NOT_FOUND
            )
        missing = 0
        for guild_id in guild_ids:
            try:
                guild = await client.fetch_guild(guild_id, with_counts=True)
                count = guild.approximate_member_count
                details.append(f"✓ {guild.name} ({guild_id})" + (f" · ~{count:,} members" if count else ""))
            except (discord.Forbidden, discord.NotFound):
                missing += 1
                details.append(f"✗ {guild_id}: the bot is not in this server, or the ID is wrong")
        if missing:
            details.append(f"Invite the bot (scope=bot): {invite_url(app_info.id)}")
            return ConnectionTestResult(
                False,
                f"{missing} of {len(guild_ids)} server{'s' if len(guild_ids) != 1 else ''} not reachable",
                details,
                ErrorKind.GUILD_NOT_FOUND,
            )
        return ConnectionTestResult(True, "Connection successful", details)
    except Exception as exc:  # noqa: BLE001
        kind, message = describe_exception(exc)
        return ConnectionTestResult(False, "Connection failed", details + [message], kind)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
