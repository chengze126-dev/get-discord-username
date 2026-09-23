# Discord Member Scout

A desktop app that watches a Discord server through your own **authorized Discord bot**. It finds members whose **server join date is before a cutoff** (default `2024-01-01 00:00:00 UTC`). It checks every 60 seconds, stores results in SQLite and sends a desktop notification for each newly found member.

Built with **Python 3.11+, PySide6, discord.py, qasync and SQLite**.

> **Official Bot API only.** The app uses a bot token, not a user token, and it is not a self-bot. It does not scrape the Discord website. A bot can only see servers it has been **explicitly invited to** by someone with *Manage Server* permission. The app cannot read any other server.

---

## Features

- Scans automatically every 60 s (configurable, minimum 60 s). Shows a live countdown and has **Scan Now**, **Pause Monitoring** and **Resume Monitoring**.
- Reads the full member list correctly for large servers. The gateway requests members in chunks, and paginated REST is the fallback. The member cache is then kept current by gateway events. The app never assumes `guild.members` is complete.
- Filters on `member.joined_at < cutoff`. Both values are timezone-aware UTC datetimes. Bots are ignored by default.
- Never stores a member twice. Each member has one row per server and Discord user ID, and later scans only refresh username, display name, avatar and channels.
- Sends **one native desktop notification per newly found member**. Delivery state is saved in SQLite, so restarting never repeats a notification. Clicking a notification brings the window to the front where the platform supports it.
- Uses a Qt model/view table (`QAbstractTableModel` + `QSortFilterProxyModel`) with avatars, instant search, year filter, 5 sort modes and CSV export of the visible rows.
- Has a slide-in details drawer with precise timestamps, a list of accessible channels, and Copy Username / Copy Discord ID buttons.
- Has a dashboard with stat cards, a scanner status panel and an activity feed that keeps the latest 100 events. There is also a full activity log and scan history page.
- Supports dark mode (default), light mode, or following the system theme.
- Keeps the bot token in the OS credential store or an environment variable. It is never written to SQLite or the logs.

---

## Requirements

- **Python 3.11 or newer**
- Windows 10/11, macOS 12+, or a Linux desktop (X11/Wayland)
- A Discord account that can create an application, and **Manage Server** permission on the target server so you can invite the bot

---

## Installation

```bash
git clone <this repository> discord-member-scout
cd discord-member-scout
```

### 1. Create a virtual environment

```bash
python -m venv venv
```

Activate it:

- **Windows (PowerShell / cmd)**

  ```powershell
  venv\Scripts\activate
  ```

- **macOS / Linux**

  ```bash
  source venv/bin/activate
  ```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Linux:** Qt needs a few system libraries. On Debian/Ubuntu run:
> `sudo apt install libegl1 libxkbcommon-x11-0 libxcb-cursor0 libdbus-1-3`.
> Desktop notifications go through the system tray / freedesktop notification service.

---

## Discord Developer Portal setup

### 1. Create the application and bot

1. Open <https://discord.com/developers/applications> and click **New Application**. Name it, e.g. *Member Scout*.
2. Open the **Bot** tab.
3. Click **Reset Token**, then copy the token. **Treat it like a password.** Anyone with the token controls the bot.
4. Optional: turn off **Public Bot** so only you can invite it.

### 2. Enable the Server Members Intent (required)

On the same **Bot** tab, under **Privileged Gateway Intents**:

- ✅ **Server Members Intent**. This is required. Without it Discord refuses the connection, and the app shows *"Missing Server Members intent"*.
- ⬜ Presence Intent is not needed.
- ⬜ Message Content Intent is not needed.

Click **Save Changes**.

> Bots in 100 or more servers must be verified by Discord before they can use privileged intents. A private bot for your own server is well below that limit.

### 3. Invite the bot to your server

1. Open **OAuth2 → URL Generator**.
2. Under **Scopes**, tick **`bot`**.
3. Under **Bot Permissions**, tick **View Channels**. That is the only permission needed; the bot never sends messages.
4. Open the generated URL, pick your server and click **Authorize**.

Or use this URL directly after replacing `YOUR_CLIENT_ID` with the *Application ID* from **General Information**:

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot&permissions=1024
```

### Required permissions

| Permission / intent | Why |
|---|---|
| `bot` scope | Lets the bot join the server |
| Server Members Intent | Lets the bot receive the member list and each member's `joined_at` |
| View Channels (`1024`) | Keeps the bot a normal, visible member. Channel accessibility is worked out locally from role and overwrite data. |

No moderation, messaging or admin permissions are needed.

### 4. Copy the server (guild) ID

In Discord, open **User Settings → Advanced → Developer Mode**. Then right-click the server icon and choose **Copy Server ID**.

---

## Configuration

You can set the token and guild ID in the app (**Settings** page) or with environment variables.

### Option A: Settings page (recommended)

Start the app, open **Settings**, paste the **Bot Token** and **Guild ID**, click **Test Connection**, then **Save Changes**.

The token is saved in your OS credential store: Windows Credential Manager, macOS Keychain, or Secret Service / KWallet on Linux.

### Option B: environment variables / `.env`

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env`:

```ini
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_GUILD_ID=123456789012345678
# SCOUT_DATA_DIR=/custom/path    (optional, defaults to ./data)
```

**Set the Guild ID automatically.** Once the bot is invited, run:

```bash
python set_guild_id.py            # lists the servers your bot is in; pick one, and .env is updated
python set_guild_id.py --name Dev # picks the server whose name contains "Dev"
python set_guild_id.py --list     # only lists the servers
```

The script uses the bot token from `.env` or the credential store (or asks for it). It only sees servers the bot has been invited to.

The Guild ID is kept in sync both ways: saving a Guild ID on the **Settings** page also writes `DISCORD_GUILD_ID` to `.env`, and on startup the value in `.env` is used. The bot token is never written to `.env` by the app.

`.env` is git-ignored. Token lookup order: OS credential store → `DISCORD_BOT_TOKEN` → a session-only token entered in the UI.

Other settings are stored in the local SQLite `settings` table:

| Setting | Default |
|---|---|
| Cutoff Date | January 1, 2024 00:00:00 UTC |
| Scan Interval | 60 seconds (minimum 60) |
| Ignore Bots | On |
| Desktop Notifications | On |
| Notification Sound | On |
| Individual Notification Limit | 10 per scan (see below) |
| Theme | Dark |

---

## Starting the application

```bash
python main.py
```

Local data lives in `data/`:

- `data/scout.db`: SQLite database (members, scan_history, activity_log, settings)
- `data/scout.log`: rotating log file, with the token redacted
- `data/avatars/`: avatar image cache

Keyboard shortcuts: **Ctrl+F** search, **Ctrl+R** scan now, **Ctrl+,** settings, **Esc** close the details drawer.

---

## How the 60-second scan cycle works

1. **Connect.** `DiscordService` opens a gateway session with only the `guilds` and `members` intents. Startup chunking of *every* server is turned off, and only the configured server is requested.
2. **Wait for the guild.** When the configured server is available, the connection turns **Connected** and the scanner becomes active. The first scan runs about a second later.
3. **Make sure the member list is complete.**
   - If `guild.chunked` is true, the cache already holds every member. Discord keeps it current with `GUILD_MEMBER_ADD/UPDATE/REMOVE` events, so a scan every minute costs **no API requests**.
   - Otherwise the app requests the full member list over the gateway (`guild.chunk()`, op 8). This happens on the first scan and after a new session. If that times out, it falls back to paging `GET /guilds/{id}/members` 1,000 at a time. discord.py handles rate limits automatically. Re-requesting members is throttled to at most once per 5 minutes.
4. **Evaluate.** Each member is checked with `joined_at < cutoff` in UTC. Bots are skipped if configured. Members without a `joined_at` are skipped. The loop hands control back to the Qt event loop every 250 members, so the UI never freezes.
5. **Resolve accessible channels.** For each match, the app works out which channels the member can view with Discord's permission rules: `@everyone`, roles, category and channel overwrites, member-specific overwrites, administrator, owner and timeout. Results are cached per scan by role signature, so thousands of members need only a few calculations.
6. **Save.** Matches are upserted into SQLite in a worker thread, keyed by `(guild_id, discord_user_id)`. New members get `first_detected_at`. Existing ones only get `last_seen_at` and changed fields updated. Stored members who have left, or no longer qualify, are marked *Not in server*. The scan is recorded in `scan_history`.
7. **Notify.** Every stored member with `notification_sent = 0` gets a desktop notification, and is then marked `notification_sent = 1`. Because this state is in SQLite, a restart never repeats notifications.
8. **Reschedule.** The next scan is due `interval` seconds after this scan *started*, so the cadence is fixed. **Scan Now** runs immediately and restarts the countdown. It is throttled to once every 5 s and never overlaps a running scan. **Pause** stops automatic scans; **Scan Now** still works.

If the connection drops, discord.py resumes the session. If the session cannot be restored, the app reconnects with **exponential backoff** (2 s → 4 s → … up to 5 min, with jitter). Retrying stops for problems a retry can't fix, such as an invalid token or missing intent, until you change Settings.

---

## Discord API limitations, and what the app does instead

| Requested | What Discord allows | What the app does |
|---|---|---|
| "Joined Channels" | Discord records when someone joined the **server**, not when they joined a channel. There is no channel-join history. | Shows the channels the member **can currently view**, from their roles and permission overwrites. The UI and CSV say so clearly. Private threads are not included. |
| Every member in large servers | Full member lists need the privileged Server Members Intent, and are delivered in chunks | Uses the intent, gateway chunking and paginated REST, and never assumes the cache is complete |
| Clicking a notification | Only some notification backends report clicks | The tray notification (Windows toast, macOS, freedesktop) brings the window forward. The plyer fallback cannot report clicks. |
| One notification per member on the very first scan | A first scan of a large server can find thousands of members. OS notification centres throttle and drop bursts. | Sends one notification per new member up to the **Individual Notification Limit** (default 10, configurable up to 1,000), then one summary for the rest. Notifications are spaced 1.5 s apart. Later scans usually find only a few new members, each notified individually. |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **Invalid Discord token** | Reset the token in Developer Portal → Bot and paste the new one in Settings. Make sure you copied the **bot token**, not the client secret. |
| **Missing Server Members intent** | Developer Portal → Bot → turn on **Server Members Intent** → Save, then restart the app or save Settings. |
| **Guild not found / bot is not in that server** | Check the Guild ID (Developer Mode → Copy Server ID) and invite the bot with the OAuth2 URL above. |
| **Network unavailable** | Check your internet, VPN, proxy or firewall. `discord.com` and `gateway.discord.gg` must be reachable. The app retries automatically. |
| **Discord rate limited** | The app waits automatically. Frequent notices usually mean several apps share the same bot token. |
| **Missing permissions (403)** | The bot's role was removed or restricted. Re-invite it with View Channels. |
| **No notifications** | Linux: make sure a notification daemon and system tray are running. Windows: allow notifications for Python in Settings → System → Notifications. macOS: allow notifications for Python in System Settings. Use **Send Test Notification** in Settings. |
| **Token can't be saved** ("kept for this session only") | No OS credential store is available (common on minimal Linux). Use `DISCORD_BOT_TOKEN` in `.env` instead. |
| **Database error** | Check that the `data/` folder is writable. Delete `data/scout.db` to start fresh. This clears history, and all current members will be notified again as new. |
| **Qt platform plugin errors on Linux** | Install the system packages listed under Installation. |

Logs are in `data/scout.log`.

---

## Project structure

```
discord-member-scout/
├── main.py                  # entry point: Qt + asyncio (qasync) loop, tray, shutdown
├── set_guild_id.py          # helper: list the bot's servers and write DISCORD_GUILD_ID to .env
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── config.py            # Settings model/store, TokenStore (keyring / env)
│   ├── controller.py        # wires DB, Discord, scanner, notifications → Qt signals
│   ├── database.py          # SQLite schema, migrations, queries
│   ├── models.py            # dataclasses & enums
│   ├── discord_service.py   # gateway connection, reconnect/backoff, member chunking, test_connection
│   ├── scanner.py           # scheduler, cutoff evaluation, channel permission resolver
│   ├── notifications.py     # native notifications (tray / plyer), pacing, sound
│   ├── avatars.py           # async avatar download + cache
│   └── utils.py             # paths, time formatting, logging with token redaction
├── ui/
│   ├── __init__.py
│   ├── theme.py             # design tokens + QSS for dark/light
│   ├── main_window.py       # window, navigation, tick timer, CSV export
│   ├── dashboard.py         # header, stat cards, scanner panel, activity feed
│   ├── member_table.py      # table model, proxy (search/filter/sort), delegates, CSV
│   ├── member_details.py    # slide-in member details drawer
│   ├── settings_dialog.py   # settings page
│   ├── activity_page.py     # activity log + scan history
│   └── components/          # sidebar, stat card, toast, toggle, status dot, avatar, icons
├── assets/
│   └── icons/               # SVG icons (recoloured at runtime)
└── data/                    # created at runtime (scout.db, logs, caches)
```

## Security notes

- The token is never written to SQLite. The `settings` table refuses token-like keys.
- A logging filter removes the token, and anything shaped like a Discord token, from every log line.
- The app requests only the `guilds` and `members` intents, and only reads data.
- There is no messaging or DM feature.
