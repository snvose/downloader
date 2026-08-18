<div align="center">

# 📥 Downloader Bot

**A Telegram bot that downloads media from 28+ platforms — just send a link.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20FreeBSD-lightgrey)

</div>

---

## ✨ What it does

Send the bot a link — YouTube, Instagram, TikTok, X/Twitter, and more — and it
downloads the media and uploads it straight back into your chat. Video or
audio, your choice. No accounts, no ads, no watermarks.

## 🌍 Supported platforms

| | | | |
|---|---|---|---|
| YouTube | YouTube Music | Instagram | TikTok |
| X / Twitter | Facebook | Reddit | Pinterest |
| Spotify¹ | SoundCloud | Vimeo | Twitch (clips) |
| Dailymotion | Streamable | Bluesky | Tumblr |
| VK | Rutube | Bilibili | Imgur |
| Bandcamp | Mixcloud | Rumble | Newgrounds |
| Loom | OK.ru | Snapchat | Kick |

<sub>¹ Spotify tracks are resolved and downloaded via YouTube — albums and playlists aren't supported.</sub>

🔴 Livestreams are intentionally not supported on any platform.

## 🚀 Install

Requires Python 3.10+, `ffmpeg`, and `git` — the installer handles the rest.

```bash
git clone <repo-url> downloader
cd downloader
bash install.sh
```

The script detects your OS, sets up a virtual environment, installs
dependencies, and walks you through configuring your bot token and admin ID.
It offers to set up autostart (systemd / OpenRC / rc.d / launchd) at the end.

Need a bot token? [@BotFather](https://t.me/BotFather) → `/newbot`.
Need your numeric ID? [@userinfobot](https://t.me/userinfobot).

Runs on Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE, Alpine, FreeBSD, and
macOS.

## ⚙️ Configuration

All settings live in `data/.env` — see [`data/example.env`](data/example.env)
for the full list with defaults.

## 🛠 Admin panel

`/admin` gives you mode switching (normal / safe / maintenance), analytics,
broadcast tools, ban management, cookie health, live logs, and premium emoji
customization — all through inline buttons.

## 🗣 Languages

English, Türkçe, Русский, Deutsch, Español, Français, العربية — switchable
from the admin panel.

## 📄 License

AGPL-3.0 — see [`LICENSE`](LICENSE). Deploying a modified version as a public
service requires sharing your source under §13; see
[`docs/COBALT.md`](docs/COBALT.md) for the note on the optional cobalt
integration.
</content>
