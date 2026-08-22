from __future__ import annotations

"""
Hourly cookie watch and the daily report to the admin.

The cookie panel only ever told the truth about the FILE: how many cookies
there are and when they expire. A file can be complete and unexpired and
still be logged out — the session the platform recognises is what matters,
and that only shows up in an actual request. So every hour this module:

  * re-reads cookies.txt (presence, expiry, the login cookie per platform)
  * asks the platforms that have a cheap endpoint whether the session is
    still recognised
  * remembers how long the current state has been going on

and once a day it sends the admin one detailed message: what is broken,
since when, how many downloads it cost, and which cookie to re-export.

State: data/cookie_watch.json
"""

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import Config
from .cookie_health import (
    PLATFORM_LOGIN_COOKIES,
    CookieLog,
    platform_cookie_status,
)
from .cookie_policy import CookieCooldown
from .storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

STATE_FILE = "cookie_watch.json"
CHECK_INTERVAL = 3600  # one hour

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# Platforms with an endpoint that answers "is this session still yours?"
# cheaply. Everything else is judged from the cookie file alone, which is
# honest about what it can know instead of guessing.
_PROBES: dict[str, dict[str, Any]] = {
    "Instagram": {
        "url": "https://i.instagram.com/api/v1/feed/timeline/",
        "headers": {"X-IG-App-ID": "936619743392459"},
    },
    "YouTube": {"url": "https://www.youtube.com/account"},
    "Reddit": {"url": "https://www.reddit.com/api/me.json"},
    "TikTok": {"url": "https://www.tiktok.com/passport/web/account/info/?aid=1459"},
    "Facebook": {"url": "https://m.facebook.com/me"},
}


def _probe(platform: str, cookies_file: Path) -> tuple[str, str]:
    """
    (state, detail) for one platform.

    state: ok | logged_out | checkpoint | unknown
    """
    spec = _PROBES.get(platform)
    if not spec or not Path(cookies_file).exists():
        return "unknown", "no live check for this platform"

    try:
        jar = MozillaCookieJar(str(cookies_file))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        return "unknown", f"cookie file unreadable: {exc}"

    headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
    headers.update(spec.get("headers", {}))

    opener = build_opener(HTTPCookieProcessor(jar))
    try:
        response = opener.open(Request(spec["url"], headers=headers), timeout=20)
        status = response.status
        body = response.read(8192).decode("utf-8", "replace")
        final_url = response.url
    except HTTPError as exc:
        status = exc.code
        body = exc.read(4096).decode("utf-8", "replace") if hasattr(exc, "read") else ""
        final_url = getattr(exc, "url", "") or ""
    except (URLError, OSError, TimeoutError) as exc:
        return "unknown", f"probe failed: {exc}"

    lowered = (body or "").lower()
    final = (final_url or "").lower()

    if "checkpoint_required" in lowered or "challenge_required" in lowered:
        return "checkpoint", "the account is locked, verification is pending"

    if status in (401, 403):
        return "logged_out", f"the platform answered {status}"

    login_markers = ("accounts/login", "servicelogin", "/login", "i/flow/login")
    if any(marker in final for marker in login_markers):
        return "logged_out", "redirected to the login page"

    if platform == "Reddit":
        try:
            data = json.loads(body)
        except ValueError:
            return "unknown", "unreadable answer"
        return ("ok", "session recognised") if data.get("data") else (
            "logged_out", "the API answered without an account",
        )

    if platform == "TikTok":
        if '"user_id"' in lowered or '"username"' in lowered:
            return "ok", "session recognised"
        return "logged_out", "the account endpoint returned no user"

    if platform == "YouTube" and "accounts.google.com" in final:
        return "logged_out", "redirected to the Google login"

    return "ok", "session recognised"


# The states that mean a human has to do something.
_BAD = {"expired", "logged_out", "checkpoint", "missing"}


def run_check(config: Config) -> dict[str, Any]:
    """
    One full check. Synchronous — call it through asyncio.to_thread.

    Returns the state written to disk.
    """
    state_file = Path(config.data_dir) / STATE_FILE
    previous = read_json(state_file, {})
    if not isinstance(previous, dict):
        previous = {}
    old_platforms = previous.get("platforms")
    if not isinstance(old_platforms, dict):
        old_platforms = {}

    cookie_log = CookieLog(config.data_dir, config.log_dir)
    failures = cookie_log.failures()
    rows = platform_cookie_status(config.cookies_file, failures=failures)

    now = time.time()
    platforms: dict[str, Any] = {}

    for row in rows:
        name = row["platform"]
        file_status = row["status"]

        # A platform with no cookies at all is not probed: the request would
        # go out anonymous and come back "logged out", which is true and
        # useless — for the optional platforms it would be a daily false
        # alarm about a session nobody asked for.
        live_state, detail = ("unknown", "")
        if row.get("count"):
            live_state, detail = _probe(name, Path(config.cookies_file))

        # The live answer wins when there is one: a file that looks perfect
        # and a session the platform no longer accepts is exactly the case
        # this watch exists for.
        if live_state in {"logged_out", "checkpoint"}:
            status = live_state
        elif live_state == "ok" and file_status == "logged_out":
            status = "ok"
        else:
            status = file_status

        old = old_platforms.get(name) if isinstance(old_platforms.get(name), dict) else {}
        since = float(old.get("since", 0)) or now
        if old.get("status") != status:
            since = now

        # A recovered platform starts clean. Otherwise the panel kept
        # reporting "25 failed requests" from a session that was refreshed
        # days ago, and the number the admin should react to was buried in
        # a total that only ever grew.
        if status == "ok" and old.get("status") in _BAD:
            cookie_log.reset(name)
            row["failures"] = 0
            row["last_reason"] = ""
            logger.info("Cookie watch: %s recovered, failure counter reset.", name)

        platforms[name] = {
            "status": status,
            "file_status": file_status,
            "live_state": live_state,
            "detail": detail,
            "since": since,
            "checked": now,
            "days_left": row.get("days_left"),
            "logged_in": row.get("logged_in"),
            "count": row.get("count", 0),
            "expired": row.get("expired", 0),
            "failures": row.get("failures", 0),
            "last_reason": row.get("last_reason", ""),
        }

    state = {
        "platforms": platforms,
        "checked": now,
        "last_report": float(previous.get("last_report", 0)),
        "last_report_day": str(previous.get("last_report_day", "")),
    }
    write_json_atomic(state_file, state)
    return state


def read_state(data_dir: Path) -> dict[str, Any]:
    state = read_json(Path(data_dir) / STATE_FILE, {})
    return state if isinstance(state, dict) else {}


_ICONS = {
    "ok": "🟢", "expiring": "🟡", "expired": "🔴", "logged_out": "🟠",
    "checkpoint": "⛔", "missing": "⚪", "optional_missing": "⚪",
}


def _age(since: float) -> str:
    seconds = max(0, int(time.time() - since))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def format_report(state: dict[str, Any], *, data_dir: Path) -> str:
    """The daily message. HTML, ready for Telegram."""
    platforms: dict[str, Any] = state.get("platforms") or {}
    broken = {n: p for n, p in platforms.items() if p.get("status") in _BAD}
    warning = {n: p for n, p in platforms.items() if p.get("status") == "expiring"}

    lines = ["🍪 <b>Daily cookie report</b>"]
    checked = state.get("checked")
    if checked:
        lines.append(
            f"<i>Last check: {time.strftime('%Y-%m-%d %H:%M', time.localtime(checked))} "
            f"— checked every hour.</i>"
        )
    lines.append("")

    if not broken and not warning:
        lines.append("Every platform has a working session. Nothing to do.")
    else:
        lines.append(f"<b>{len(broken)} platform(s) need attention.</b>")

    for name, entry in sorted(
        platforms.items(),
        key=lambda kv: (kv[1].get("status") not in _BAD, kv[0]),
    ):
        status = entry.get("status", "unknown")
        icon = _ICONS.get(status, "⚪")
        row = f"{icon} <b>{html.escape(name)}</b> — {html.escape(status.replace('_', ' '))}"

        if status in _BAD:
            row += f", for {_age(float(entry.get('since') or time.time()))}"
        elif status == "expiring" and entry.get("days_left") is not None:
            days = int(entry["days_left"])
            row += ", expires today" if days <= 0 else f", {days} day(s) left"

        lines.append(row)

        if status in _BAD or status == "expiring":
            detail = str(entry.get("detail") or "")
            if detail and entry.get("live_state") in {"logged_out", "checkpoint"}:
                lines.append(f"    └ {html.escape(detail)}")
            failures = int(entry.get("failures") or 0)
            if failures:
                reason = str(entry.get("last_reason") or "")
                lines.append(
                    f"    └ {failures} failed download(s)"
                    + (f" — {html.escape(reason)}" if reason else "")
                )
            needed = ", ".join(PLATFORM_LOGIN_COOKIES.get(name, [])) or "-"
            if status in {"logged_out", "expired", "missing"}:
                lines.append(f"    └ needed cookie: <code>{html.escape(needed)}</code>")
            if status == "checkpoint":
                lines.append(
                    "    └ re-exporting the cookie is not enough, the account "
                    "owner has to pass the verification first"
                )

    cooldowns = CookieCooldown(data_dir).entries()
    if cooldowns:
        lines.append("")
        lines.append("<b>Rate limited recently</b> (requests kept anonymous):")
        for name, entry in cooldowns.items():
            left = max(0, int(float(entry.get("until", 0)) - time.time()) // 60)
            lines.append(f"• {html.escape(name)} — {left} min left")

    if broken:
        lines.append("")
        lines.append(
            "Export cookies.txt again from a browser that is logged in to "
            "the platforms above, then upload it from the cookie panel."
        )

    return "\n".join(lines)


async def cookie_watch_scheduler(bot: Any, config: Config) -> None:
    """
    Checks every hour, reports once a day.

    The report is sent at config.cookie_report_hour; the day is recorded, so
    a restart cannot make it arrive twice.
    """
    logger.info(
        "Cookie watch active: hourly check, daily report at %02d:00 (UTC%+d).",
        config.cookie_report_hour, config.cleanup_tz_offset,
    )

    # A first check right after start, so the panel is never empty.
    await asyncio.sleep(60)

    while True:
        try:
            state = await asyncio.to_thread(run_check, config)

            tz = timezone(timedelta(hours=config.cleanup_tz_offset))
            now = datetime.now(tz)
            today = now.strftime("%Y-%m-%d")

            platforms = state.get("platforms") or {}
            needs_attention = any(
                entry.get("status") in _BAD or entry.get("status") == "expiring"
                for entry in platforms.values()
                if isinstance(entry, dict)
            )

            due = (
                now.hour >= config.cookie_report_hour % 24
                and str(state.get("last_report_day") or "") != today
            )

            # A daily "everything is fine" is noise the admin learns to
            # ignore, and then misses the one that matters. The report is
            # sent when something actually needs a hand.
            if due and not needs_attention:
                state["last_report_day"] = today
                await asyncio.to_thread(
                    write_json_atomic, Path(config.data_dir) / STATE_FILE, state,
                )
                logger.info("Cookie watch: every platform healthy, no report sent.")

            elif due and config.admin_id:
                text = format_report(state, data_dir=config.data_dir)
                try:
                    await bot.send_message(
                        chat_id=config.admin_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    state["last_report"] = time.time()
                    state["last_report_day"] = today
                    await asyncio.to_thread(
                        write_json_atomic, Path(config.data_dir) / STATE_FILE, state,
                    )
                    logger.info("Daily cookie report sent to the admin.")
                except Exception as exc:
                    logger.warning("Could not send the cookie report: %s", exc)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Cookie watch error")

        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
