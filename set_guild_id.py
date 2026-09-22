"""Find the servers your bot is in and write the chosen ID to .env as DISCORD_GUILD_ID.

Uses the official Bot API (GET /users/@me/guilds), so it only lists servers
the bot has been invited to. No gateway connection is opened.

Usage:
    python set_guild_id.py                 # list servers and pick one
    python set_guild_id.py --name "Dev"    # pick the server whose name contains "Dev"
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
EXAMPLE_PATH = ROOT / ".env.example"
KEY = "DISCORD_GUILD_ID"

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


async def fetch_guilds(token: str) -> list[tuple[int, str, int | None]]:
    import discord

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
        guilds = []
        async for guild in client.fetch_guilds(limit=None, with_counts=True):
            guilds.append((guild.id, guild.name, guild.approximate_member_count))
        return guilds
    finally:
        await client.close()


def write_guild_id(guild_id: int) -> None:
    """Set DISCORD_GUILD_ID in .env, keeping every other line unchanged."""
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    elif EXAMPLE_PATH.exists():
        lines = EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    new_line = f"{KEY}={guild_id}"
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if stripped.startswith(f"{KEY}=") or stripped == KEY:
            if not replaced:
                lines[index] = new_line
                replaced = True
    if not replaced:
        lines.append(new_line)

    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)


def choose(guilds: list[tuple[int, str, int | None]], name: str | None) -> tuple[int, str, int | None] | None:
    if name:
        matches = [g for g in guilds if name.casefold() in g[1].casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            print(f'No server name contains "{name}".')
            return None
        print(f'Several servers match "{name}"; pick one:')
        guilds = matches
    if len(guilds) == 1:
        return guilds[0]
    while True:
        answer = input(f"Enter a number 1-{len(guilds)} (or q to quit): ").strip().lower()
        if answer in ("q", "quit", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(guilds):
            return guilds[int(answer) - 1]
        print("Invalid choice.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", help="select the server whose name contains this text")
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
        print("The bot is not in any server yet. Invite it first:")
        print("  https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot&permissions=1024")
        return 1

    guilds.sort(key=lambda g: g[1].casefold())
    print("\nServers this bot is in:\n")
    for number, (gid, name, count) in enumerate(guilds, start=1):
        members = f"  ~{count:,} members" if count else ""
        print(f"  {number:>2}. {name}  (ID {gid}){members}")
    print()

    if args.list:
        return 0

    picked = choose(guilds, args.name)
    if picked is None:
        print(".env was not changed.")
        return 1

    gid, name, _ = picked
    write_guild_id(gid)
    print(f'Saved {KEY}={gid} ("{name}") to {ENV_PATH}')
    print("Note: if you also set a Guild ID on the app's Settings page, that value wins. Clear it there to use .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
