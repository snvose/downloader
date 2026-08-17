from __future__ import annotations

"""
Livestream protection.

Livestream links produce an endless stream: the download never finishes, the
worker slot stays busy forever and the disk keeps growing. This module detects
a livestream before the download starts and rejects it.

Two parts:
  1) probe_is_live() — fast metadata query, no download
  2) LiveGuard       — warnings and a temporary ban for repeat attempts
"""

import time
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic


# ── 1. Detection ─────────────────────────────────────────────────────────────

_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}


def info_is_live(info: dict[str, Any] | None) -> bool:
    """Does this yt-dlp info dict describe a livestream?"""
    if not isinstance(info, dict):
        return False

    if info.get("is_live") is True:
        return True

    if str(info.get("live_status") or "") in _LIVE_STATUSES:
        return True

    # Playlist entries: one live entry rejects the whole thing.
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries[:20]:
            if isinstance(entry, dict) and info_is_live(entry):
                return True

    return False


def probe_is_live(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: int = 15,
) -> tuple[bool, dict[str, Any]]:
    """
    Asks whether the link is a livestream without downloading anything.

    Returns (is_live, info). A failed query returns (False, {}) so uncertainty
    never blocks a download; normal error handling takes over.
    """
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": timeout,
        "retries": 1,
        "extract_flat": "in_playlist",
    }

    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = str(cookies_file)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception:
        return False, {}

    if not isinstance(info, dict):
        return False, {}

    return info_is_live(info), info


# ── 2. Warnings and temporary bans ───────────────────────────────────────────

class LiveGuard:
    """
    Gradually restricts a user who keeps sending livestream links.

    Attempts 1 and 2 produce a warning, attempt `strike_limit` produces a
    temporary ban of `ban_days` days that lifts itself when it expires.

    State lives in data/temp_bans.json:
        {"users": {"<id>": {"strikes": 2, "until": 0.0, "last": 172...}}}
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        strike_limit: int = 3,
        ban_days: int = 5,
        strike_window_days: int = 7,
    ):
        self.file = Path(data_dir) / "temp_bans.json"
        self.strike_limit = int(strike_limit)
        self.ban_days = int(ban_days)
        self.strike_window = int(strike_window_days) * 86400

    def _load(self) -> dict[str, dict]:
        data = read_json(self.file, {"users": {}})
        if not isinstance(data, dict):
            return {}
        users = data.get("users")
        return users if isinstance(users, dict) else {}

    def _save(self, users: dict[str, dict]) -> None:
        write_json_atomic(self.file, {"users": users})

    def ban_remaining(self, user_id: int | None) -> float:
        """Remaining ban time in seconds; 0 means not banned."""
        if not user_id:
            return 0.0

        users = self._load()
        record = users.get(str(user_id))
        if not isinstance(record, dict):
            return 0.0

        until = float(record.get("until") or 0.0)
        remaining = until - time.time()

        if remaining <= 0:
            if until:
                record["until"] = 0.0
                record["strikes"] = 0
                users[str(user_id)] = record
                self._save(users)
            return 0.0

        return remaining

    def is_banned(self, user_id: int | None) -> bool:
        return self.ban_remaining(user_id) > 0

    def register_attempt(self, user_id: int) -> dict[str, Any]:
        """
        Records a livestream attempt.

        Returns either
          {"action": "warn",   "strikes": n, "remaining": left}
          {"action": "banned", "strikes": n, "days": d, "seconds": s}
        """
        users = self._load()
        key = str(user_id)
        record = users.get(key) if isinstance(users.get(key), dict) else {}

        now = time.time()
        strikes = int(record.get("strikes") or 0)
        last = float(record.get("last") or 0.0)

        # A user who stayed clean for a long time starts over.
        if last and (now - last) > self.strike_window:
            strikes = 0

        strikes += 1
        record["strikes"] = strikes
        record["last"] = now

        if strikes >= self.strike_limit:
            seconds = self.ban_days * 86400
            record["until"] = now + seconds
            users[key] = record
            self._save(users)
            return {
                "action": "banned",
                "strikes": strikes,
                "days": self.ban_days,
                "seconds": seconds,
            }

        record["until"] = 0.0
        users[key] = record
        self._save(users)
        return {
            "action": "warn",
            "strikes": strikes,
            "remaining": self.strike_limit - strikes,
        }

    def clear(self, user_id: int) -> bool:
        """Admin action: clears the ban and the strike counter."""
        users = self._load()
        if str(user_id) not in users:
            return False
        users.pop(str(user_id), None)
        self._save(users)
        return True

    def list_active(self) -> list[dict[str, Any]]:
        now = time.time()
        out: list[dict[str, Any]] = []
        for key, record in self._load().items():
            if not isinstance(record, dict):
                continue
            until = float(record.get("until") or 0.0)
            if until > now:
                out.append({
                    "user_id": int(key) if key.lstrip("-").isdigit() else key,
                    "remaining": until - now,
                    "strikes": int(record.get("strikes") or 0),
                })
        return sorted(out, key=lambda x: -x["remaining"])


def guard_message(result: dict[str, Any]) -> str:
    """Turns a register_attempt() result into a user-facing message."""
    from .i18n import t

    if result.get("action") == "banned":
        return t("live_temp_banned", days=int(result.get("days", 5)))

    remaining = int(result.get("remaining", 0))
    if remaining <= 1:
        return t("live_last_warning")
    return t("live_not_supported")


def format_duration(seconds: float) -> str:
    """Human readable remaining ban time."""
    from .i18n import t

    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        return f"{days} {t('unit_days')} {hours} {t('unit_hours')}" if hours else f"{days} {t('unit_days')}"
    if hours:
        return f"{hours} {t('unit_hours')} {minutes} {t('unit_minutes')}" if minutes else f"{hours} {t('unit_hours')}"
    return f"{max(1, minutes)} {t('unit_minutes')}"
