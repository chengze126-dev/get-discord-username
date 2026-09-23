"""Find the servers your bot is in and write the chosen IDs to .env as DISCORD_GUILD_IDS.

Uses the official Bot API (GET /users/@me/guilds), so it only lists servers
the bot has been invited to. No gateway connection is opened.

Usage:
    python set_guild_id.py                 # list servers and pick one or more (e.g. "1,3" or "a" for all)
    python set_guild_id.py --all           # select every server the bot is in
    python set_guild_id.py --name "Dev"    # select the servers whose name contains "Dev"
    python set_guild_id.py --list          # only list servers, don't change .env

Token lookup: DISCORD_BOT_TOKEN in .env / environment, then the OS credential
store (set from the app's Settings page), then a hidden prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
KEY = "DISCORD_GUILD_IDS"
LEGACY_KEY = "DISCORD_GUILD_ID"

sys.path.insert(0, str(ROOT))


def resolve_token() -> str:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        from app.config import TokenStore

        token = TokenStore().get() or ""
    except Exception:  # noqa: BLE001 - credential store is optional
        token = ""
    if token:
        return token
    return getpass.getpass("Bot token (input hidden): ").strip()


INVITE_URL = ""


async def fetch_guilds(token: str) -> list[tuple[int, str, int | None]]:
    import discord

    from app.discord_service import invite_url

    global INVITE_URL
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
        guilds = []
        async for guild in client.fetch_guilds(limit=None, with_counts=True):
            guilds.append((guild.id, guild.name, guild.approximate_member_count))
        INVITE_URL = invite_url((await client.application_info()).id)
        return guilds
    finally:
        await client.close()


def write_guild_ids(guild_ids: list[int]) -> None:
    """Set DISCORD_GUILD_IDS in .env (and clear the old single-server key), keeping other lines."""
    from app.config import format_guild_ids, write_env_value

    write_env_value(KEY, format_guild_ids(guild_ids), ENV_PATH)
    if os.environ.get(LEGACY_KEY, "").strip():
        write_env_value(LEGACY_KEY, "", ENV_PATH)


Guild = tuple[int, str, int | None]


def choose(guilds: list[Guild], name: str | None, select_all: bool) -> list[Guild]:
    if select_all:
        return guilds
    if name:
        matches = [g for g in guilds if name.casefold() in g[1].casefold()]
        if not matches:
            print(f'No server name contains "{name}".')
        return matches
    if len(guilds) == 1:
        return guilds
    while True:
        answer = input(f"Enter numbers 1-{len(guilds)} separated by commas, 'a' for all, or q to quit: ").strip().lower()
        if answer in ("q", "quit", ""):
            return []
        if answer in ("a", "all"):
            return guilds
        parts = [p.strip() for p in answer.replace(" ", ",").split(",") if p.strip()]
        if parts and all(p.isdigit() and 1 <= int(p) <= len(guilds) for p in parts):
            picked = list(dict.fromkeys(int(p) for p in parts))
            return [guilds[i - 1] for i in picked]
        print("Invalid choice.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", help="select the servers whose name contains this text")
    parser.add_argument("--all", action="store_true", help="select every server the bot is in")
    parser.add_argument("--list", action="store_true", help="only list servers")
    args = parser.parse_args()

    token = resolve_token()
    if not token:
        print("No bot token found. Put DISCORD_BOT_TOKEN in .env or enter it in the app's Settings.")
        return 1

    import discord

    try:
        guilds = asyncio.run(fetch_guilds(token))
    except discord.LoginFailure:
        print("Discord rejected the token. Reset it in the Developer Portal (Bot tab) and try again.")
        return 1
    except discord.HTTPException as exc:
        print(f"Discord API error (HTTP {exc.status}): {exc.text}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Discord: {type(exc).__name__}: {exc}")
        return 1

    if not guilds:
        print("The token works, but the bot is not in any server yet. Open this link, pick your server and")
        print("click Authorize (you need Manage Server permission on that server):")
        print(f"  {INVITE_URL}")
        print("A link with only scope=applications.commands does NOT add the bot.")
        return 1

    guilds.sort(key=lambda g: g[1].casefold())
    print("\nServers this bot is in:\n")
    for number, (gid, name, count) in enumerate(guilds, start=1):
        members = f"  ~{count:,} members" if count else ""
        print(f"  {number:>2}. {name}  (ID {gid}){members}")
    print()

    if args.list:
        return 0

    picked = choose(guilds, args.name, args.all)
    if not picked:
        print(".env was not changed.")
        return 1

    write_guild_ids([gid for gid, _name, _count in picked])
    print(f"Saved {KEY}={','.join(str(g[0]) for g in picked)} to {ENV_PATH}")
    for gid, name, _count in picked:
        print(f"  • {name} ({gid})")
    print("Restart the app to use these servers. The Settings page will then show them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
