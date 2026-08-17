# Downloader Bot

A multi-process Telegram bot that downloads media from 28 platforms and
uploads it to Telegram. It has a clean, professional interface, a persistent
cache, run modes, and an admin panel.

## Supported Platforms

The bot uses three download sources: **cobalt** (optional, your own
instance), **yt-dlp**, and **gallery-dl**. If a source fails, the next one is
tried; the order is configurable per platform via `data/sources.json`.

| Platform | Video | Audio | Source order | Status |
|----------|:-----:|:-----:|---------------|--------|
| YouTube | ✅ 1080p–360p | ✅ 320k–128k | yt-dlp → cobalt | ✅ tested |
| YouTube Music | — | ✅ | yt-dlp → cobalt | ✅ tested |
| Instagram | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ tested |
| TikTok | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ tested |
| X/Twitter | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ tested |
| Reddit | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ tested |
| Pinterest | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ tested |
| Facebook | ✅ | — | cobalt → yt-dlp → gallery-dl | ✅ tested |
| Spotify | — | ✅ (via YouTube) | yt-dlp | ✅ tested |
| **SoundCloud** | — | ✅ | yt-dlp → cobalt | ✅ tested |
| **Dailymotion** | ✅ | ✅ | yt-dlp → cobalt | ✅ tested |
| **Streamable** | ✅ | ✅ | yt-dlp → cobalt | ✅ tested |
| **Vimeo** | ✅ | ✅ | yt-dlp → cobalt | ⚠️ temporary yt-dlp issue |
| **Twitch (clips)** | ✅ | ✅ | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Bluesky** | ✅ | ✅ | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Tumblr** | ✅ | ✅ | yt-dlp → gallery-dl | ⏳ not end-to-end tested |
| **VK** | ✅ | — | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Rutube** | ✅ | ✅ | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Bilibili** | ✅ | ✅ | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Imgur** | ✅ | — | yt-dlp → gallery-dl | ⏳ not end-to-end tested |
| **Bandcamp** | — | ✅ | yt-dlp | ⏳ not end-to-end tested |
| **Mixcloud** | — | ✅ | yt-dlp | ⏳ not end-to-end tested |
| **Rumble** | ✅ | ✅ | yt-dlp | ⏳ not end-to-end tested |
| **Newgrounds** | ✅ | ✅ | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Loom** | ✅ | — | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **OK.ru** | ✅ | — | yt-dlp → cobalt | ⏳ not end-to-end tested |
| **Snapchat** | ✅ | ✅ | cobalt → yt-dlp | ⏳ not end-to-end tested |
| **Kick** | ✅ | ✅ | yt-dlp | ⏳ not end-to-end tested |

**Bold** entries were added in Phase 2. "⏳" means the underlying tools support
the platform but it hasn't been verified end-to-end with a real link.

🔴 **Livestreams are not supported on any platform** — they produce an
endless stream, so they're rejected before a download ever starts.

> yt-dlp recognizes 1751 sites, gallery-dl 269. The list above is
> deliberately kept narrow: only commonly used platforms that fit the bot's
> interface are enabled. To add a new one, update `SUPPORTED_DOMAINS` and
> `platform_name()` in `bot/utils.py`.

## Features

- **Multi-source downloading** — cobalt + yt-dlp + gallery-dl; a failure
  automatically falls through to the next source (configured via
  `data/sources.json`).
- **Format selection** — a video (1080p–360p) / audio (320k–128k) menu for
  YouTube.
- **Subtitles** — download YouTube videos with EN/TR subtitles burned in.
- **Accurate audio metadata** — title, artist, album artist, album, release
  year and genre are read from the source and written to the file (mutagen
  ID3/MP4). The cover is cropped to a square and embedded. A field missing
  from the source is left empty, never guessed.
- **Broadcast system** — send a bulk message from the panel; audience
  selection, live progress, a summary report. Chats that blocked the bot are
  marked and skipped on the next broadcast. Users can opt out with
  `/broadcasts`.
- **Analytics dashboard** — DAU/WAU/MAU, a 7-day trend chart, platform and
  source distribution, most active users. Activity writes are buffered (no
  disk write per message).
- **User search + ban management** — search by username/ID/title from the
  panel, view a profile, manage bans and livestream bans.
- **Log viewer** — 4 channels, a level filter, raw file download.
- **Cookie health panel** — `/admin → 🍪 Cookies`: per-platform cookie status
  (valid / expiring soon / expired / missing), days remaining, and the
  number of requests that failed because of it. Cookie-related errors are
  also written to `data/logs/cookie_errors.log`.
- **Livestream protection** — live links are rejected within 2 seconds;
  repeated attempts get a warning, the 3rd triggers a 5-day temporary ban.
- **SQLite database** — user/chat records, first seen, last activity, usage
  counts and broadcast preference (`data/bot.db`).
- **Playlist browser** — selective or bulk downloads from YouTube playlists.
- **Clean interface** — only the platform logo and name appear under the
  media; details expand behind a single "Details" button.
- **file_id cache** — sending the same link twice doesn't re-download it;
  it's delivered instantly via the stored `file_id`, re-uploading from disk
  only if needed.
- **Automatic cleanup** — the `downloads/` folder is cleared every day at a
  configured time (default 00:00 UTC); the number of files removed and space
  freed is logged.
- **Detailed logging** — every download is written to daily log files with
  user, chat, platform, result, size and duration (kept for 7 days by
  default).
- **Multi-language** — user-facing messages in 7 languages: English,
  Türkçe, Русский, Deutsch, Español, Français, العربية. The language is
  chosen from the `/admin` panel.
- **Run modes** — `normal`, `safe` (silent), `maintenance`.
- **Full admin panel** — `/admin`: switch modes, pick a language, start/stop,
  statistics, system status, chat/group usage, and clearing active jobs —
  all through buttons.
- **Premium emoji management** — a categorized panel, per-slot assignment,
  bulk reset, and import/export.
- **Failure notifications** — if a download can't complete, the admin gets a
  summary, the last 20 log lines, and quick mode-switch buttons.
- **Local Bot API support** — optional; needed to send files over 50 MB.

## Supported Systems

Runs on every common system except Windows, auto-detected by the install
script:

| System | Autostart |
|--------|-----------|
| Debian / Ubuntu / Mint | systemd |
| Fedora / RHEL / Rocky / Alma | systemd |
| openSUSE | systemd |
| Arch / Manjaro | systemd |
| Alpine / Gentoo | OpenRC |
| FreeBSD | rc.d |
| macOS | launchd |

> **Requirements:** Python 3.10+, `ffmpeg` (for audio downloads), `git`.
> `gallery-dl` and `yt-dlp` are installed into the virtual environment during
> setup.

## Quick Install

```bash
git clone <repo-url> downloader
cd downloader
bash install.sh
```

The install script:

1. Detects the OS and package manager.
2. Installs Python, ffmpeg and git.
3. Creates a virtual environment (`venv`) and installs dependencies.
4. Asks for the bot token, admin ID, bot name and similar settings.
5. Optionally configures the start menu links, Local Bot API, and autostart.

### Token and Admin ID

- **Bot Token:** [@BotFather](https://t.me/BotFather) → `/newbot`
- **Admin ID:** [@userinfobot](https://t.me/userinfobot) gives you your
  numeric ID.

## Configuration

All settings are read from `data/.env`. See
[`data/example.env`](data/example.env) for a template.

| Variable | Description | Default |
|----------|--------------|---------|
| `BOT_TOKEN` | BotFather token (required) | — |
| `ADMIN_ID` | Admin's Telegram ID (required) | — |
| `BOT_NAME` | The bot's display name | `Downloader` |
| `SHOW_LINKS` | Show links in the start menu | `false` |
| `OWNER_LINK` / `COMMUNITY_LINK` | Owner / community links | empty |
| `MAX_SIMULTANEOUS_DOWNLOADS` | Concurrent download limit | `3` |
| `MAX_FILE_SIZE_MB` | Max file size | `1900` |
| `LOCAL_BOT_API_BASE` | Local Bot API address (optional) | empty |
| `CLEANUP_TZ_OFFSET` / `CLEANUP_HOUR` | Cleanup time / UTC offset | `0` / `0` |
| `LOG_RETENTION_DAYS` | Log retention (days) | `7` |
| `CACHE_ENABLED` | file_id cache | `true` |
| `JOB_TIMEOUT_SEC` | Hard time limit per download (s) | `1800` |
| `JOB_MAX_GB` | Hard disk limit per download (GB) | `4` |
| `LIVE_STRIKE_LIMIT` | Livestream attempts before a ban | `3` |
| `LIVE_BAN_DAYS` | Temporary ban length (days) | `5` |
| `COBALT_API_URL` | Your own cobalt instance address (optional) | empty |
| `COBALT_API_KEY` | cobalt API key (if required) | empty |
| `COBALT_TIMEOUT` | cobalt request timeout (s) | `30` |
| `DB_PATH` | SQLite database path | `data/bot.db` |

### Download source priority

`data/sources.json` decides which source is tried first, per platform. The
file can be edited by hand; a new order takes effect **without restarting**
the bot.

```json
{
  "default": ["ytdlp", "cobalt", "gallerydl"],
  "platforms": {
    "TikTok":    ["cobalt", "ytdlp", "gallerydl"],
    "YouTube":   ["ytdlp", "cobalt"]
  }
}
```

cobalt has **no public API**; you must run your own instance. If
`COBALT_API_URL` is empty, cobalt is silently skipped and the bot runs
normally on yt-dlp. Setup and the AGPL-3.0 licensing note:
[`docs/COBALT.md`](docs/COBALT.md).

## Database

`data/bot.db` (SQLite) — user and chat records, download history.

| Table | Contents |
|-------|----------|
| `users` | user_id, username, first seen, last activity, download count, broadcast preference, blocked status |
| `chats` | chat_id, title, type, first seen, last activity, download count |
| `chat_platforms` | per-chat platform usage counter |
| `downloads` | full history of every download (platform, source, size, duration, result) |

Existing `chats.json` / `usage_stats.json` data is migrated automatically on
first launch (the JSON files are not deleted). For a future move to
Postgres, all SQL lives in one file (`bot/db.py`) and avoids SQLite-specific
syntax.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome menu |
| `/help` | Help and supported platforms |
| `/audio <link>` | Download the link as audio |
| `/cancel` | Cancel the active download |
| `/broadcasts` | Turn bot announcements on / off |
| `/admin` | (Admin) mode switching and usage panel |
| `/status` | (Admin) system status |
| `/stop` · `/resume` | (Admin) stop / start the bot |
| `/banid` · `/unbanid` | (Admin) ban / unban a user or group |

## Run Modes

- **normal** — regular operation.
- **safe** — silent mode. No messages/notifications are sent to the user;
  a link is downloaded quietly and only the media file is sent as a reply.
- **maintenance** — no downloads; every request gets a fixed message.

The mode is switched from the `/admin` panel, persisted in
`data/bot_state.json`, and survives restarts.

## Admin Panel (`/admin`)

| Screen | Contents |
|--------|----------|
| Main panel | Mode (normal/safe/maintenance), start/stop, active download counter |
| 📈 Analytics | DAU/WAU/MAU, 7-day trend, platform/source distribution, success rate |
| 🏆 Most Active | Users with the most downloads |
| 💬 Usage | Chat/group usage list (paginated) |
| 🚫 Bans | Ban list + user search → profile → ban management |
| 🍪 Cookies | Per-platform cookie status, days remaining, failure counter, error log |
| 📣 Broadcast | Audience selection, message composer, preview, sending, summary |
| 📜 Logs | Live stream / bot.log / downloads.log / cookie_errors.log + filter |
| 🎨 Emoji | Premium emoji slot management (labeled by where it appears) |
| 🖥 System | Versions, ffmpeg/gallery-dl status, limits |

Irreversible actions (clear jobs, reset everything, ban) ask for
confirmation first.

## Language

The bot shows every user-facing string (status messages, buttons, error
descriptions, media info) in the selected language. Supported languages:
`en`, `tr`, `ru`, `de`, `es`, `fr`, `ar`. The language is chosen from the
`/admin` panel and stored in `data/bot_state.json`.

## Premium Emoji Management

The editor, reachable via `/emojis` or from the panel, lets you replace the
bot's icons with
[Telegram premium custom emoji](https://core.telegram.org/bots/api#messageentity):

- Slots are grouped into categories (menu, platform icons, info fields,
  buttons, status, owner).
- Send a premium emoji to the bot, then tap a slot to assign it.
- Reset one slot with `♻️`, or everything at once with **🗑 Reset all**.
- Download a backup of `emoji_slots.json` with **📤 Download backup**.

> Assigning a premium emoji is optional; unassigned slots fall back to a
> regular emoji.

## Service Management (systemd)

```bash
sudo systemctl status downloader-bot     # status
sudo systemctl restart downloader-bot    # restart
sudo systemctl stop downloader-bot       # stop
journalctl -u downloader-bot -f          # live log
```

## Running Manually

```bash
venv/bin/python start.py
```

## Project Structure

```
bot/
  app.py               Application setup, event loop
  config.py             .env configuration
  process_manager.py    Multi-process download manager
  downloader/            yt-dlp / gallery-dl / cobalt download logic
  handlers/              Command, message and button handlers
  sender.py              Sending media to Telegram + cache
  cache.py                file_id cache
  chats.py                Usage statistics
  state.py                Run mode management
  scheduler.py            Daily cleanup
  ui.py                   Message and keyboard templates
start.py                 Entry point
install.sh               Automated installer
```

## Notes

- Sending files over 50 MB requires a
  [Local Bot API](https://github.com/tdlib/telegram-bot-api) server, enabled
  via `LOCAL_BOT_API_BASE`.
- For content that requires login, add `data/cookies.txt` (Netscape format).

## License

AGPL-3.0. See [`LICENSE`](LICENSE). If you deploy a modified version of this
bot as a network service for others, AGPL-3.0 §13 requires you to make the
modified source available to those users. cobalt integration is a separate
consideration — see [`docs/COBALT.md`](docs/COBALT.md).
