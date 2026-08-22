from __future__ import annotations

"""
Cookie health tracking and reporting.

1) FILE ANALYSIS — reads cookies.txt per platform: which platform has cookies,
   how many are expired, when the nearest one expires.

2) FAILURE TRACKING — separates cookie related download failures, writes them
   to data/logs/cookie_errors.log and counts them per platform in
   data/cookie_stats.json, so the admin panel can answer "which cookie do I
   need to refresh?" at a glance.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

# Error patterns that indicate the cookies need refreshing.
_COOKIE_ERROR_PATTERNS = [
    # First: an account lock is NOT fixed by refreshing cookies, the account
    # owner has to complete the challenge. Matched before the generic rules so
    # the panel does not say "refresh the cookie".
    (r"checkpoint_required|challenge_required|instagram\.com/challenge",
     "account locked — verification required (refreshing cookies is not enough)"),
    (r"sign in to confirm", "sign-in confirmation requested"),
    (r"login required", "login required"),
    (r"available to everyone|certain audiences|comfortable for some audiences",
     "content restricted to certain viewers — a logged-in session is needed"),
    (r"requested content is not available|content isn'?t available", "content hidden without a session"),
    (r"private (video|account|profile)", "private content"),
    (r"this video is only available for registered users", "members only"),
    (r"http error 401", "unauthorized (401)"),
    (r"http error 403|403: forbidden", "access denied (403)"),
    (r"age.?restricted|confirm your age", "age restricted"),
    # A bare "cookies" match produced false positives: the combined error text
    # also contains attempt LABELS ("cookies: ERROR: ...", "cookies-loose: ..."),
    # which turned unrelated failures into "cookie errors".
    (r"--cookies|cookies? (?:are |is )?(?:no longer valid|invalid|expired|"
     r"rejected|required)|invalid cookies?|cookies? (?:have )?expired",
     "cookie error"),
    (r"unable to download webpage.*(login|auth)", "session problem"),
    (r"rate.?limit|too many requests", "rate limited (session should be refreshed)"),
    (r"empty media response", "empty response (session may have expired)"),
    (r"unable to extract (shared_data|sharedData|viewer)", "session cookie invalid"),
]

# Which cookie domains each platform needs.
PLATFORM_DOMAINS = {
    "YouTube": ["youtube.com"],
    "YouTube Music": ["youtube.com"],
    "Instagram": ["instagram.com"],
    "TikTok": ["tiktok.com"],
    "Facebook": ["facebook.com"],
    "X/Twitter": ["x.com", "twitter.com"],
    "Reddit": ["reddit.com"],
    "Pinterest": ["pinterest.com"],
    "Spotify": ["spotify.com"],
}

# The cookie that actually carries the login. A file can be full of fresh
# cookies for a domain and still be anonymous without one of these — the case
# that made "everything is ok" show in the panel while every age-gated
# Instagram post failed.
PLATFORM_LOGIN_COOKIES = {
    "Instagram": ["sessionid"],
    "TikTok": ["sessionid", "sessionid_ss"],
    "Facebook": ["xs"],
    "X/Twitter": ["auth_token"],
    "Reddit": ["reddit_session"],
    "Pinterest": ["_auth"],
    "Spotify": ["sp_dc"],
    "YouTube": ["SID", "__Secure-3PSID", "__Secure-1PSID"],
    "YouTube Music": ["SID", "__Secure-3PSID", "__Secure-1PSID"],
}

# These platforms work without a login, so missing cookies are not an error.
_OPTIONAL_COOKIE_PLATFORMS = {
    "YouTube", "YouTube Music", "Reddit", "Pinterest", "Spotify",
}

EXPIRY_WARN_DAYS = 7

# Cookies that are SUPPOSED to be short lived: a bot check token, the
# browser window size, a tracking id. Warning that they expire soon said
# "refresh your cookies" every single day about a session that was fine, so
# the expiry warning looks at the cookies that carry the login instead.
_EPHEMERAL_COOKIES = {
    "__cf_bm", "wd", "dpr", "session_tracker", "csrf_token", "_dd_s",
    "ttwid_expire", "tt_chain_token", "gads", "gpi",
}


def _session_expiry(
    expiry_by_name: dict[str, int],
    login_cookies: list[str],
) -> int:
    """
    When does the login itself run out?

    The login cookies decide when the platform names one; otherwise the
    earliest expiry that isn't a throwaway cookie.
    """
    if login_cookies:
        candidates = [
            ts for name, ts in expiry_by_name.items()
            if name in login_cookies and ts > 0
        ]
        if candidates:
            return min(candidates)

    candidates = [
        ts for name, ts in expiry_by_name.items()
        if ts > 0 and name not in _EPHEMERAL_COOKIES
    ]
    return min(candidates) if candidates else 0


# A download can run through a different platform than the link's own
# (Spotify resolves through a YouTube search), so the cookie to refresh is
# whichever platform the error itself points at.
_ERROR_PLATFORM_MARKERS = [
    (r"\[youtube(:search)?\]|youtube\.com|ytsearch", "YouTube"),
    (r"\[instagram\]|instagram\.com", "Instagram"),
    (r"\[tiktok\]|tiktok\.com", "TikTok"),
    (r"\[facebook\]|facebook\.com", "Facebook"),
    (r"\[twitter\]|\[x\]|twitter\.com|(?<!\w)x\.com", "X/Twitter"),
    (r"\[reddit\]|reddit\.com", "Reddit"),
    (r"\[pinterest\]|pinterest\.com", "Pinterest"),
]


def error_platform_hint(message: str) -> str | None:
    """Which platform's cookie does this error point at?"""
    if not message:
        return None

    lowered = str(message).lower()
    for pattern, platform in _ERROR_PLATFORM_MARKERS:
        if re.search(pattern, lowered):
            return platform
    return None


def classify_cookie_error(message: str) -> str | None:
    """Returns a readable reason when the error is cookie related, else None."""
    if not message:
        return None

    lowered = str(message).lower()
    for pattern, reason in _COOKIE_ERROR_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return None


# ── 1. File analysis ─────────────────────────────────────────────────────────

def parse_cookie_file(path: Path) -> dict[str, dict[str, Any]]:
    """
    Summarises a Netscape cookies.txt per domain.

    Returns {"tiktok.com": {"count": 22, "expired": 0, "nearest_expiry": ts,
                            "names": [...]}}
    """
    path = Path(path)
    result: dict[str, dict[str, Any]] = {}

    if not path.exists():
        return result

    now = time.time()

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()
        if len(parts) < 7:
            continue

        domain = parts[0].lstrip(".").lower()
        try:
            expiry = int(parts[4])
        except (TypeError, ValueError):
            expiry = 0
        name = parts[5]

        entry = result.setdefault(domain, {
            "count": 0, "expired": 0, "nearest_expiry": 0, "names": [],
            "expiry_by_name": {},
        })
        entry["count"] += 1
        if name not in entry["names"]:
            entry["names"].append(name)
        if expiry > 0:
            current = entry["expiry_by_name"].get(name)
            entry["expiry_by_name"][name] = (
                expiry if current is None else min(current, expiry)
            )

        if expiry > 0:
            if expiry < now:
                entry["expired"] += 1
            else:
                current = entry["nearest_expiry"]
                entry["nearest_expiry"] = expiry if not current else min(current, expiry)

    return result


def platform_cookie_status(
    cookies_file: Path,
    *,
    failures: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Cookie status per platform for the admin panel.

    status: ok | expiring | expired | missing | optional_missing
    """
    domains = parse_cookie_file(cookies_file)
    failures = failures or {}
    now = time.time()
    rows: list[dict[str, Any]] = []

    for platform, wanted in PLATFORM_DOMAINS.items():
        matched: dict[str, Any] | None = None
        names: list[str] = []

        # Subdomains count too (www.tiktok.com -> tiktok.com)
        expiry_by_name: dict[str, int] = {}

        for domain, entry in domains.items():
            if any(domain == w or domain.endswith("." + w) for w in wanted):
                if matched is None:
                    matched = {"count": 0, "expired": 0, "nearest_expiry": 0}
                names.extend(entry["names"])
                for cookie_name, ts in entry.get("expiry_by_name", {}).items():
                    current = expiry_by_name.get(cookie_name)
                    expiry_by_name[cookie_name] = (
                        ts if current is None else min(current, ts)
                    )
                matched["count"] += entry["count"]
                matched["expired"] += entry["expired"]
                nearest = entry["nearest_expiry"]
                if nearest:
                    matched["nearest_expiry"] = (
                        nearest if not matched["nearest_expiry"]
                        else min(matched["nearest_expiry"], nearest)
                    )

        fail_count = int((failures.get(platform) or {}).get("count", 0))
        last_reason = str((failures.get(platform) or {}).get("reason", ""))
        last_time = float((failures.get(platform) or {}).get("last", 0))

        if not matched:
            status = (
                "optional_missing" if platform in _OPTIONAL_COOKIE_PLATFORMS else "missing"
            )
            rows.append({
                "platform": platform, "status": status, "count": 0,
                "expired": 0, "nearest_expiry": 0, "days_left": None,
                "logged_in": False,
                "failures": fail_count, "last_reason": last_reason,
                "last_failure": last_time,
            })
            continue

        login_cookies = PLATFORM_LOGIN_COOKIES.get(platform, [])
        nearest = _session_expiry(expiry_by_name, login_cookies) or matched["nearest_expiry"]
        days_left = int((nearest - now) / 86400) if nearest else None

        logged_in = not login_cookies or any(name in names for name in login_cookies)

        if matched["expired"] and matched["expired"] >= matched["count"]:
            status = "expired"
        elif not logged_in and platform not in _OPTIONAL_COOKIE_PLATFORMS:
            # Cookies are there and unexpired, but none of them is the session.
            status = "logged_out"
        elif days_left is not None and days_left <= EXPIRY_WARN_DAYS:
            status = "expiring"
        else:
            status = "ok"

        rows.append({
            "platform": platform,
            "status": status,
            "count": matched["count"],
            "expired": matched["expired"],
            "nearest_expiry": nearest,
            "days_left": days_left,
            "logged_in": logged_in,
            "failures": fail_count,
            "last_reason": last_reason,
            "last_failure": last_time,
        })

    # Problems first: expired -> logged out -> missing -> expiring -> ok
    order = {
        "expired": 0, "logged_out": 1, "missing": 2,
        "expiring": 3, "optional_missing": 4, "ok": 5,
    }
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["failures"]))
    return rows


# ── 2. Failure tracking ──────────────────────────────────────────────────────

class CookieLog:
    """
    Writes cookie related failures to their own channel and counts them.

        data/logs/cookie_errors.log — detailed, human readable
        data/cookie_stats.json      — per platform counters for the panel
    """

    def __init__(self, data_dir: Path, log_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.log_dir = Path(log_dir) if log_dir else self.data_dir / "logs"
        self.log_file = self.log_dir / "cookie_errors.log"
        self.stats_file = self.data_dir / "cookie_stats.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.stats_file, {"platforms": {}})
        if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
            return {"platforms": {}}
        return data

    def record(
        self,
        *,
        platform: str,
        reason: str,
        url: str = "",
        error: str = "",
        user_id: int | None = None,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        domains = ", ".join(PLATFORM_DOMAINS.get(platform, [])) or "-"

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            line = (
                f"{timestamp} | platform={platform} | needs_cookie={domains} "
                f"| reason={reason} | user={user_id or '-'} | url={url[:160]} "
                f"| error={str(error)[:300].replace(chr(10), ' ')}\n"
            )
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            logger.warning("Could not write the cookie log: %s", exc)

        try:
            data = self._load()
            entry = data["platforms"].setdefault(platform, {"count": 0})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["reason"] = reason
            entry["last"] = time.time()
            entry["last_url"] = url[:200]
            write_json_atomic(self.stats_file, data)
        except Exception as exc:
            logger.warning("Could not write the cookie stats: %s", exc)

    def failures(self) -> dict[str, Any]:
        return self._load().get("platforms", {})

    def total(self) -> int:
        return sum(
            int(v.get("count", 0))
            for v in self.failures().values()
            if isinstance(v, dict)
        )

    def reset(self, platform: str | None = None) -> None:
        data = self._load()
        if platform:
            data["platforms"].pop(platform, None)
        else:
            data["platforms"] = {}
        write_json_atomic(self.stats_file, data)

    def tail(self, lines: int = 15) -> list[str]:
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        return [ln for ln in content.splitlines() if ln.strip()][-lines:]
