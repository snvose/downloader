from __future__ import annotations

"""
Cookie policy and anti-bot request shaping.

Three jobs:

1) WHEN TO SEND THE COOKIE — a public reel, a public tweet or a public
   YouTube video downloads perfectly well logged out. Sending the session on
   those requests buys nothing and costs a lot: every authenticated request
   ties this server's IP to one account, and that is exactly the pattern the
   platforms score as "bot". So cookies are used when the content actually
   needs them (stories, private posts, age gates) or as a FALLBACK after the
   anonymous attempt is refused, not as the default first move.

2) HOW THE REQUEST LOOKS — a real browser is recognised by its TLS handshake
   first and its headers second. With curl_cffi present yt-dlp can copy
   Chrome's handshake; the header set here matches the same browser so the
   two halves tell the same story. The user agent is stable per platform per
   day rather than random per request: a client whose fingerprint changes
   mid-session is more suspicious than a boring one.

3) BACKING OFF — when a platform does answer with a rate limit or a bot
   check, the account is what gets burned. The cooldown below drops that
   platform back to anonymous requests for a while and slows them down,
   instead of retrying with the session until it is locked.
"""

import hashlib
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import read_json, write_json_atomic
from .utils import instagram_story_kind, platform_name

# Cookies buy nothing here — the content is public and the session only adds
# a fingerprint. Cookies stay available as a fallback attempt.
_PUBLIC_FIRST = {
    "YouTube", "YouTube Music", "TikTok", "X/Twitter", "Twitter",
    "Reddit", "Pinterest", "Spotify",
}

# Almost nothing on these is readable logged out, so the anonymous attempt is
# just a wasted round trip.
_COOKIE_FIRST = {"Facebook"}

# URL shapes that genuinely need a session, whatever the platform default is.
_LOGIN_WALLED_PATTERNS = (
    r"/stories/",
    r"/s/aGlnaGxpZ2h0",      # highlight permalinks
    r"/private/",
)


def _platform(url: str) -> str:
    return platform_name(url)


def needs_cookies(url: str) -> bool:
    """True when the link cannot be fetched without a session at all."""
    path = (urlparse(url).path or "")
    if instagram_story_kind(url):
        return True
    return any(re.search(pattern, path, re.IGNORECASE) for pattern in _LOGIN_WALLED_PATTERNS)


class CookiePreference:
    """
    Remembers which request style actually worked per platform.

    The static policy above is a good first guess, not a fact: this server's
    IP can be anonymous-friendly on one platform and blocked on another, and
    that changes over weeks. So whichever style last produced a file goes
    first next time, and the guess is only used until there is evidence.
    """

    TTL = 12 * 3600

    def __init__(self, data_dir: Path):
        self.file = Path(data_dir) / "cookie_pref.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.file, {"platforms": {}})
        if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
            return {"platforms": {}}
        return data

    def record_success(self, platform: str, used_cookies: bool) -> None:
        if not platform:
            return
        try:
            data = self._load()
            data["platforms"][platform] = {"cookies": bool(used_cookies), "at": time.time()}
            write_json_atomic(self.file, data)
        except Exception:
            pass

    def preferred(self, platform: str) -> bool | None:
        entry = self._load()["platforms"].get(platform)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("at", 0)) > self.TTL:
            return None
        return bool(entry.get("cookies"))


def cookie_order(url: str, *, data_dir: Path | None = None) -> tuple[bool, ...]:
    """
    The order the two request styles are tried in: True = with cookies.

    Both are always present — the fallback is what keeps a public-first
    policy from losing content — only the order changes.
    """
    if needs_cookies(url):
        return (True, False)

    platform = _platform(url)

    if data_dir is not None:
        learned = CookiePreference(data_dir).preferred(platform)
        if learned is not None:
            return (learned, not learned)

    if platform in _COOKIE_FIRST:
        return (True, False)
    if platform in _PUBLIC_FIRST:
        return (False, True)

    # Instagram and anything unknown: public content first, session second.
    return (False, True)


# ── Request shaping ──────────────────────────────────────────────────────────

# One family, several plausible machines. Chrome on Windows/macOS is the most
# common thing a platform sees, so it is the least interesting thing to be.
#
# The Chrome VERSION is pinned rather than rotated, and it is not the newest
# one on purpose. TikTok serves a different page per user agent version, and
# yt-dlp's extractor can only read one of them: measured here, 139 works
# while 138, 140 and 145 all come back as "Unexpected response from webpage
# request" or "Unable to download webpage". Only the operating system
# rotates, which every version handled identically.
#
# So when TikTok starts failing across the board after a yt-dlp upgrade,
# this is the first thing to re-check.
_CHROME_VERSION = "139"

_USER_AGENTS = (
    (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36", '"Windows"'),
    (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36", '"macOS"'),
    (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36", '"Linux"'),
)


def _agent_for(platform: str) -> tuple[str, str]:
    """Stable per platform per day: same browser all day, not a new one per request."""
    day = int(time.time() // 86400)
    digest = hashlib.sha256(f"{platform}:{day}".encode()).digest()
    return _USER_AGENTS[digest[0] % len(_USER_AGENTS)]


def browser_headers(url: str) -> dict[str, str]:
    """A header set that matches the impersonated browser."""
    agent, platform_hint = _agent_for(_platform(url))
    major = _CHROME_VERSION
    return {
        "User-Agent": agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": (
            f'"Chromium";v="{major}", "Not;A=Brand";v="24", '
            f'"Google Chrome";v="{major}"'
        ),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": platform_hint,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


# Platforms where the TLS fingerprint is checked. YouTube is left on yt-dlp's
# own handler on purpose: its extractor does its own client negotiation and
# impersonation gains nothing there.
_IMPERSONATE_PLATFORMS = {
    "Instagram", "TikTok", "X/Twitter", "Twitter", "Facebook",
    "Reddit", "Pinterest",
}


# Whether this install can actually impersonate anything. Asking yt-dlp
# costs a handler probe, so it is asked once per process.
_IMPERSONATION_AVAILABLE: bool | None = None


def _impersonation_available(target: Any) -> bool:
    """
    Is the target really usable here?

    Handing yt-dlp a target it cannot serve does not degrade — it raises
    before the request is made, so an install without curl_cffi (or with a
    version yt-dlp silently refuses, see requirements.txt) would fail every
    social download instead of just losing the disguise.
    """
    global _IMPERSONATION_AVAILABLE

    if _IMPERSONATION_AVAILABLE is None:
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                available = [item[0] for item in ydl._get_available_impersonate_targets()]
            _IMPERSONATION_AVAILABLE = any(target in candidate for candidate in available)
        except Exception:
            _IMPERSONATION_AVAILABLE = False

    return _IMPERSONATION_AVAILABLE


def impersonate_target(url: str) -> Any | None:
    """yt-dlp ImpersonateTarget for this link, or None when not applicable."""
    if _platform(url) not in _IMPERSONATE_PLATFORMS:
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception:
        return None

    target = ImpersonateTarget("chrome")
    return target if _impersonation_available(target) else None


def pacing(url: str) -> dict[str, Any]:
    """
    A short pause between requests on the platforms that count them.

    Costs about a second on a normal download and keeps a burst of metadata
    requests from looking like a scraper.
    """
    if _platform(url) in _IMPERSONATE_PLATFORMS:
        return {"sleep_interval_requests": 1}
    return {}


# ── Cooldown after a rate limit / bot check ──────────────────────────────────

_BOT_CHECK_PATTERNS = (
    r"rate.?limit", r"too many requests", r"http error 429",
    r"sign in to confirm", r"confirm you'?re not a bot",
    r"unusual activity", r"suspicious", r"temporarily blocked",
    r"please wait a few minutes", r"challenge_required", r"checkpoint_required",
)

COOLDOWN_SECONDS = 45 * 60


def is_bot_check_error(message: str) -> bool:
    """Did the platform answer with a rate limit or a bot check?"""
    lowered = str(message or "").lower()
    return any(re.search(pattern, lowered) for pattern in _BOT_CHECK_PATTERNS)


class CookieCooldown:
    """
    Remembers which platform just pushed back, so the next few jobs stay
    anonymous there instead of spending the session on a wall.

    File backed because downloads run in separate processes.
    """

    def __init__(self, data_dir: Path):
        self.file = Path(data_dir) / "cookie_cooldown.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.file, {"platforms": {}})
        if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
            return {"platforms": {}}
        return data

    def mark(self, platform: str, reason: str = "") -> None:
        if not platform:
            return
        try:
            data = self._load()
            data["platforms"][platform] = {
                "until": time.time() + COOLDOWN_SECONDS,
                "reason": str(reason)[:200],
            }
            write_json_atomic(self.file, data)
        except Exception:
            pass

    def active(self, platform: str) -> bool:
        entry = self._load()["platforms"].get(platform)
        if not isinstance(entry, dict):
            return False
        return float(entry.get("until", 0)) > time.time()

    def entries(self) -> dict[str, Any]:
        now = time.time()
        return {
            name: entry
            for name, entry in self._load()["platforms"].items()
            if isinstance(entry, dict) and float(entry.get("until", 0)) > now
        }
