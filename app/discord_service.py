"""Discord gateway connection management (official Bot API only).

The service owns a :class:`discord.Client` that runs on the shared asyncio
loop (driven by Qt through qasync). It reports state changes to the UI through
Qt signals and reconnects with exponential backoff when the connection cannot
be (re-)established.

Only the ``guilds`` and ``members`` gateway intents are requested. ``members``
is the privileged *Server Members Intent* that must be enabled in the Discord
Developer Portal; without it Discord refuses the connection (close code 4014)
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

from app.models import ConnectionState, ErrorKind

log = logging.getLogger(__name__)

BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 300.0
# Discord throttles REQUEST_GUILD_MEMBERS (gateway op 8). Once a guild is chunked,
# the member cache is kept current by GUILD_MEMBER_ADD/UPDATE/REMOVE events, so
# re-chunking is only needed after a fresh session, and never more than this often.
CHUNK_COOLDOWN_SECONDS = 300.0
RATE_LIMIT_NOTICE_COOLDOWN = 30.0


@dataclass(frozen=True)
class GuildInfo:
    id: int
    name: str
    member_count: int
    cached_members: int


@dataclass
class ConnectionTestResult:
    ok: bool
    title: str
    details: list[str] = field(default_factory=list)
    error_kind: ErrorKind | None = None


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True   # guild, channel and role data (needed for permission checks)
    intents.members = True  # privileged: Server Members Intent
    return intents


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


class DiscordService(QObject):
    state_changed = Signal(str, str)        # ConnectionState value, detail text
    guild_changed = Signal(object)          # GuildInfo | None
    error_occurred = Signal(str, str)       # ErrorKind value, message
    ready_changed = Signal(bool)            # True once the configured guild is available

    def __init__(self, token_provider: Callable[[], str | None], guild_id: int | None) -> None:
        super().__init__()
        self._token_provider = token_provider
        self._guild_id = guild_id
        self._client: discord.Client | None = None
        self._runner_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._state = ConnectionState.DISCONNECTED
        self._guild_ready = False
        self._last_chunk_at: float = 0.0
        self._chunk_lock = asyncio.Lock()
        self._last_rate_notice = 0.0
        self._rate_handler = _RateLimitLogHandler(self._on_rate_limit_logged)
        logging.getLogger("discord.http").addHandler(self._rate_handler)
        logging.getLogger("discord.gateway").addHandler(self._rate_handler)

    # ----------------------------------------------------------------- public

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def guild_id(self) -> int | None:
        return self._guild_id

    @property
    def is_ready(self) -> bool:
        return self._guild_ready and self.guild is not None

    @property
    def guild(self) -> discord.Guild | None:
        if self._client is None or self._guild_id is None or not self._client.is_ready():
            return None
        guild = self._client.get_guild(self._guild_id)
        if guild is None or guild.unavailable:
            return None
        return guild

    def start(self) -> None:
        if self._runner_task is None or self._runner_task.done():
            self._stopping = False
            self._runner_task = asyncio.ensure_future(self._run_forever())

    def set_guild_id(self, guild_id: int | None) -> None:
        if guild_id == self._guild_id:
            return
        self._guild_id = guild_id
        self._last_chunk_at = 0.0
        if self._client is not None and self._client.is_ready():
            asyncio.ensure_future(self._resolve_guild())

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

    async def ensure_member_cache(self, guild: discord.Guild) -> tuple[list[discord.Member], bool]:
        """Return the guild's members, fetching them if the cache is incomplete.

        Returns ``(members, complete)``. ``complete`` is True only when the list
        is known to contain every member (chunked via gateway or fully paged
        over REST).
        """
        async with self._chunk_lock:
            if guild.chunked:
                return list(guild.members), True

            now = time.monotonic()
            if self._last_chunk_at and now - self._last_chunk_at < CHUNK_COOLDOWN_SECONDS:
                # Recently chunked; the small drift is caused by joins/leaves in flight.
                members = list(guild.members)
                expected = guild.member_count or len(members)
                return members, len(members) >= expected * 0.99

            expected = guild.member_count or 0
            timeout = max(60.0, expected / 1000 * 3)
            log.info("Requesting full member list for guild %s (%s members)", guild.id, expected)
            try:
                await asyncio.wait_for(guild.chunk(cache=True), timeout=timeout)
                self._last_chunk_at = time.monotonic()
                self._emit_guild_info()
                return list(guild.members), True
            except asyncio.TimeoutError:
                log.warning("Gateway chunking timed out; falling back to paginated REST fetch")

            # REST fallback: GET /guilds/{id}/members, 1000 per page (discord.py paginates
            # and honours rate-limit headers automatically).
            members: list[discord.Member] = []
            async for member in guild.fetch_members(limit=None):
                members.append(member)
                if len(members) % 5000 == 0:
                    await asyncio.sleep(0)
            self._last_chunk_at = time.monotonic()
            return members, True

    # --------------------------------------------------------------- internal

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        self.state_changed.emit(state.value, detail)

    def _set_guild_ready(self, ready: bool) -> None:
        if ready != self._guild_ready:
            self._guild_ready = ready
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

    def _emit_guild_info(self) -> None:
        guild = self.guild
        if guild is None:
            self.guild_changed.emit(None)
            return
        self.guild_changed.emit(
            GuildInfo(
                id=guild.id,
                name=guild.name,
                member_count=guild.member_count or len(guild.members),
                cached_members=len(guild.members),
            )
        )

    async def _wait_for_wake(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _make_client(self) -> discord.Client:
        client = discord.Client(
            intents=build_intents(),
            chunk_guilds_at_startup=False,  # only the configured guild is chunked, on demand
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.from_intents(build_intents()),
            max_ratelimit_timeout=60.0,
        )

        @client.event
        async def on_ready() -> None:
            log.info("Logged in as %s (id %s)", client.user, getattr(client.user, "id", "?"))
            await self._resolve_guild()

        @client.event
        async def on_resumed() -> None:
            await self._resolve_guild()

        @client.event
        async def on_disconnect() -> None:
            if self._stopping:
                return
            self._set_guild_ready(False)
            self._set_state(ConnectionState.DISCONNECTED, "Connection lost — reconnecting…")

        @client.event
        async def on_guild_available(guild: discord.Guild) -> None:
            if guild.id == self._guild_id:
                await self._resolve_guild()

        @client.event
        async def on_guild_unavailable(guild: discord.Guild) -> None:
            if guild.id == self._guild_id:
                self._set_guild_ready(False)
                self._set_state(ConnectionState.ERROR, "Guild temporarily unavailable (Discord outage)")

        @client.event
        async def on_guild_join(guild: discord.Guild) -> None:
            if guild.id == self._guild_id:
                await self._resolve_guild()

        @client.event
        async def on_guild_remove(guild: discord.Guild) -> None:
            if guild.id == self._guild_id:
                self._set_guild_ready(False)
                self._set_state(ConnectionState.ERROR, "Bot was removed from the guild")
                self._emit_error(
                    ErrorKind.GUILD_NOT_FOUND,
                    f"The bot was removed from '{guild.name}'. Re-invite it to continue monitoring.",
                )

        @client.event
        async def on_member_join(member: discord.Member) -> None:
            if member.guild.id == self._guild_id:
                self._emit_guild_info()

        @client.event
        async def on_member_remove(member: discord.Member) -> None:
            if member.guild.id == self._guild_id:
                self._emit_guild_info()

        @client.event
        async def on_error(event_method: str, *args, **kwargs) -> None:
            log.exception("Unhandled error in Discord event %s", event_method)

        return client

    async def _resolve_guild(self) -> None:
        client = self._client
        if client is None or not client.is_ready():
            return
        if self._guild_id is None:
            self._set_guild_ready(False)
            self._set_state(ConnectionState.ERROR, "No Guild ID configured")
            self._emit_error(ErrorKind.GUILD_NOT_FOUND, "Set the Server (Guild) ID in Settings to start monitoring.")
            return

        guild = client.get_guild(self._guild_id)
        if guild is None:
            detail = (
                f"The bot is not a member of guild {self._guild_id}, or the ID is wrong. "
                "Invite the bot to that server with the OAuth2 URL from the Developer Portal."
            )
            try:
                fetched = await client.fetch_guild(self._guild_id)
                detail = f"Guild '{fetched.name}' is not available to this bot's gateway session yet."
            except (discord.Forbidden, discord.NotFound):
                pass
            except discord.HTTPException as exc:
                detail = describe_exception(exc)[1]
            self._set_guild_ready(False)
            self._set_state(ConnectionState.ERROR, "Guild not found")
            self._emit_error(ErrorKind.GUILD_NOT_FOUND, detail)
            self.guild_changed.emit(None)
            return

        if guild.unavailable:
            self._set_guild_ready(False)
            self._set_state(ConnectionState.ERROR, "Guild temporarily unavailable")
            return

        self._set_state(ConnectionState.CONNECTED, guild.name)
        self._emit_guild_info()
        self._set_guild_ready(True)

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
            client = self._make_client()
            self._client = client
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
                self._set_guild_ready(False)
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


async def test_connection(token: str, guild_id: int | None) -> ConnectionTestResult:
    """Validate credentials over REST only (no gateway session is opened)."""
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
        has_members_intent = bool(flags.gateway_guild_members or flags.gateway_guild_members_limited)
        if not has_members_intent:
            return ConnectionTestResult(
                False,
                "Server Members Intent is disabled",
                details
                + [
                    "Enable 'Server Members Intent' in the Developer Portal → Bot → Privileged Gateway Intents.",
                ],
                ErrorKind.MISSING_INTENT,
            )
        details.append("Server Members Intent is enabled")

        if guild_id is None:
            return ConnectionTestResult(False, "Guild ID required", details + ["Enter the Server (Guild) ID."], ErrorKind.GUILD_NOT_FOUND)
        try:
            guild = await client.fetch_guild(guild_id, with_counts=True)
        except (discord.Forbidden, discord.NotFound):
            return ConnectionTestResult(
                False,
                "Bot is not in that server",
                details
                + [
                    f"Guild {guild_id} does not exist or the bot has not been invited to it.",
                    "Invite the bot with the OAuth2 URL Generator (scope: bot).",
                ],
                ErrorKind.GUILD_NOT_FOUND,
            )
        count = guild.approximate_member_count
        details.append(f"Guild: {guild.name}" + (f" · ~{count:,} members" if count else ""))
        return ConnectionTestResult(True, "Connection successful", details)
    except Exception as exc:  # noqa: BLE001
        kind, message = describe_exception(exc)
        return ConnectionTestResult(False, "Connection failed", details + [message], kind)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
