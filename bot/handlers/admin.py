from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import sys
import time
from datetime import datetime

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot import analytics
from bot.broadcast import BroadcastJob, run_broadcast, validate_html
from bot.i18n import LANGUAGES, set_language
from bot.live_guard import format_duration
from bot.cookie_health import platform_cookie_status
from bot.cookie_policy import CookieCooldown
from bot.cookie_watch import read_state
from bot.emoji_manager import broadcast_slots, em, render_slots
from bot.pending import clear_all_pending
from bot.safe_message import safe_reply
from bot.state import MODE_MAINTENANCE, MODE_NORMAL, MODE_SAFE


logger = logging.getLogger("downloader")


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _admin_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    permissions = context.application.bot_data["permissions"]
    return permissions.is_admin(update.effective_user.id)


async def dur_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    permissions.set_bot_enabled(False)
    manager.shutdown()

    await safe_reply(update.message, "Bot stopped. Active jobs were cancelled.")


async def basla_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    permissions = context.application.bot_data["permissions"]
    permissions.set_bot_enabled(True)

    await safe_reply(update.message, "Bot started.")


async def banid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await safe_reply(
            update.message,
            "Usage: <code>/banid ID</code>\n\n"
            "Positive ID → user, negative ID → group/channel.",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID must be numeric.")
        return

    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    # The id's sign decides the right list. Every id used to go to "users",
    # so a group id landed there and group bans never matched anything.
    if permissions.ban_id(target_id):
        cancelled = manager.cancel_chat_jobs(target_id)
        note = f" ({cancelled} active downloads cancelled)" if cancelled else ""
        await safe_reply(update.message, f"Group banned: {target_id}{note}")
    else:
        manager.cancel_user_job(target_id)
        await safe_reply(update.message, f"User banned: {target_id}")


async def unbanid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await safe_reply(
            update.message,
            "Usage: <code>/unbanid ID</code>\n\n"
            "Positive ID → user, negative ID → group/channel.",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID must be numeric.")
        return

    permissions = context.application.bot_data["permissions"]
    kind = "Group" if permissions.unban_id(target_id) else "User"

    await safe_reply(update.message, f"{kind} unbanned: {target_id}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    config = context.application.bot_data["config"]
    manager = context.application.bot_data["process_manager"]
    permissions = context.application.bot_data["permissions"]

    active_jobs = [
        job for job in manager.jobs.values()
        if not job.done and not job.cancelled
    ]

    counts = permissions.counts()

    ffmpeg = "yes" if shutil.which("ffmpeg") else "no"
    gallery_dl = "yes" if shutil.which("gallery-dl") else "no"

    text = (
        f"<b>{config.bot_name} — Status</b>\n\n"
        f"Bot state: <b>{'running' if counts['enabled'] else 'stopped'}</b>\n"
        f"Local Bot API: <b>{'on' if config.local_bot_api_base else 'off'}</b>\n"
        f"Active downloads: <b>{len(active_jobs)}</b>\n"
        f"Total job records: <b>{len(manager.jobs)}</b>\n"
        f"Max concurrent: <b>{config.max_simultaneous_downloads}</b>\n"
        f"Max file size: <b>{config.max_file_size_mb} MB</b>\n\n"
        f"Banned users: <b>{counts['banned_users']}</b>\n"
        f"Banned groups: <b>{counts['banned_groups']}</b>\n\n"
        f"Python: <code>{sys.version.split()[0]}</code>\n"
        f"yt-dlp: <code>{yt_dlp.version.__version__}</code>\n"
        f"ffmpeg: <b>{ffmpeg}</b>\n"
        f"gallery-dl: <b>{gallery_dl}</b>\n\n"
        f"Download directory:\n<code>{config.download_dir}</code>"
    )

    await safe_reply(update.message,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return
    manager = context.application.bot_data["process_manager"]
    manager.shutdown()
    # Also delete pending menu messages — otherwise they stayed on screen,
    # clickable, with no job behind them anymore.
    cleared = await clear_all_pending(context.application)
    context.application.bot_data["playlist_sessions"] = {}
    await safe_reply(update.message, f"All jobs cleared. ({cleared} menus removed)")


# ── /admin panel ─────────────────────────────────────────────────────────────

_MODE_LABEL = {
    MODE_NORMAL: "🟢 Normal",
    MODE_SAFE: "🔇 Safe mode (silent)",
    MODE_MAINTENANCE: "🛠 Maintenance mode",
}

_MODE_SHORT = {
    MODE_NORMAL: "🟢 Normal",
    MODE_SAFE: "🔇 Safe",
    MODE_MAINTENANCE: "🛠 Maint.",
}


# Telegram's message length limit. A longer message is not sent at all.
_TELEGRAM_TEXT_LIMIT = 4096

# HTML tags Telegram recognizes in parse_mode="HTML" and that must be closed.
_HTML_TAGS = ("b", "i", "u", "s", "code", "pre", "a", "blockquote")


def _fit(text: str) -> str:
    """
    Fits panel text into Telegram's limit.

    The log view used to cap 15 lines at 220 chars each, but HTML escaping
    (& -> &amp;) and <code> tags can inflate the text past 4096 after a job
    with a long traceback, so the panel simply never updated
    ("Message_too_long"). The cut happens at a line boundary, a half-open tag
    is dropped, and any tags left open are closed — otherwise Telegram would
    reject it with "can't parse entities" instead.
    """
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text

    notice = "\n\n<i>… message truncated.</i>"
    cut = text[: _TELEGRAM_TEXT_LIMIT - len(notice)]

    line_break = cut.rfind("\n")
    if line_break > len(cut) // 2:
        cut = cut[:line_break]

    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]

    for tag in _HTML_TAGS:
        if cut.count(f"<{tag}>") + cut.count(f"<{tag} ") > cut.count(f"</{tag}>"):
            cut += f"</{tag}>"

    return cut + notice


async def _edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    """
    Updates the panel message.

    Every exception used to be swallowed silently: if the update failed the
    admin got no feedback at all when tapping a button. Now "no change"
    (harmless) is separated out, real errors are logged and the admin is
    warned.
    """
    text = _fit(text)
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "not modified" in message:
            return

        # If HTML can't be parsed (e.g. a log line happened to look like a
        # tag), send an unformatted but readable version rather than losing
        # the panel entirely.
        if "parse entities" in message or "parse_mode" in message:
            try:
                plain = html.unescape(re.sub(r"<[^>]+>", "", text))
                await query.edit_message_text(
                    plain[:_TELEGRAM_TEXT_LIMIT],
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                pass

        logger.warning("Admin panel update failed: %s", exc)
        try:
            await query.answer(
                "Panel update failed, reopen with /admin.", show_alert=True
            )
        except Exception:
            pass


# ── Main panel ──
def _panel_keyboard(state) -> InlineKeyboardMarkup:
    mode = state.get_mode()
    enabled = state.get_enabled()
    mode_row = [
        InlineKeyboardButton(
            ("🔘 " if mode == m else "⚪️ ") + _MODE_SHORT[m].split(" ", 1)[1],
            callback_data=f"admin|mode|{m}",
        )
        for m in (MODE_NORMAL, MODE_SAFE, MODE_MAINTENANCE)
    ]
    return InlineKeyboardMarkup([
        mode_row,
        [InlineKeyboardButton(
            "⏸ Stop bot" if enabled else "▶️ Start bot",
            callback_data="admin|toggle",
        )],
        [
            InlineKeyboardButton("🌐 Language", callback_data="admin|langmenu"),
            InlineKeyboardButton("📈 Analytics", callback_data="admin|analytics"),
            InlineKeyboardButton("🖥 System", callback_data="admin|status"),
        ],
        [
            InlineKeyboardButton("💬 Usage", callback_data="admin|usage|0"),
            InlineKeyboardButton("🚫 Bans", callback_data="admin|bans"),
            InlineKeyboardButton("🍪 Cookies", callback_data="admin|cookie"),
        ],
        [
            InlineKeyboardButton("📣 Broadcast", callback_data="admin|broadcast"),
            InlineKeyboardButton("📜 Logs", callback_data="admin|logs|live|all"),
            InlineKeyboardButton("🎨 Emoji", callback_data="emoji|page|0"),
        ],
        [
            InlineKeyboardButton("🧹 Clear jobs", callback_data="admin|clear"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="admin|panel"),
            InlineKeyboardButton("✖️ Close", callback_data="admin|close"),
        ],
    ])


_MODE_HINT = {
    MODE_NORMAL: "regular operation",
    MODE_SAFE: "silent — media only, no messages or buttons",
    MODE_MAINTENANCE: "downloads off — a fixed notice is shown",
}


def _panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    state = context.application.bot_data["bot_state"]
    manager = context.application.bot_data["process_manager"]
    config = context.application.bot_data["config"]
    # Numbers come from a SINGLE source (the DB). They used to be read from
    # chats.json in the panel, usage_stats.json in the stats screen and the
    # DB in analytics — the same number could disagree in three places.
    db = context.application.bot_data.get("db")
    s = db.stats() if db else {"total_chats": 0, "groups": 0, "privates": 0, "total_downloads": 0}
    mode = state.get_mode()
    lang = state.get_language()
    enabled = state.get_enabled()
    active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
    limit = config.max_simultaneous_downloads
    bar = "▰" * active + "▱" * max(0, limit - active)
    return (
        f"<b>⚙️ {config.bot_name} · Admin Panel</b>\n"
        "──────────────────\n"
        f"{'🟢' if enabled else '⏸'} State: <b>{'Running' if enabled else 'Stopped'}</b>\n"
        f"{_MODE_LABEL.get(mode, mode).split(' ')[0]} Mode: <b>{_MODE_LABEL.get(mode, mode).split(' ', 1)[1]}</b>\n"
        f"      <i>{_MODE_HINT.get(mode, '')}</i>\n"
        f"🌐 Language: <b>{LANGUAGES.get(lang, lang)}</b>\n"
        f"⚡ Active downloads: <b>{active}/{limit}</b>  <code>{bar}</code>\n"
        "──────────────────\n"
        f"💬 Chats: <b>{s['total_chats']}</b> "
        f"(groups <b>{s['groups']}</b> · private <b>{s['privates']}</b>)\n"
        f"📥 Total downloads: <b>{s['total_downloads']}</b>"
    )


# ── Language menu ──
def _language_keyboard(current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, name in LANGUAGES.items():
        mark = "✅ " if code == current else ""
        row.append(InlineKeyboardButton(f"{mark}{name}", callback_data=f"admin|lang|{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])
    return InlineKeyboardMarkup(rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("‹ Panel", callback_data="admin|panel")]])


def _cookie_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 Error log", callback_data="admin|cookielog"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin|cookie"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


def _cookie_log_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧹 Reset counters", callback_data="admin|cookiereset"),
            InlineKeyboardButton("‹ Cookies", callback_data="admin|cookie"),
        ],
    ])


# ── Cookie status ──
_COOKIE_STATUS_ICON = {
    "expired": "🔴",
    "missing": "🔴",
    "logged_out": "🟠",
    "checkpoint": "⛔",
    "expiring": "🟠",
    "optional_missing": "⚪️",
    "ok": "🟢",
}

_COOKIE_STATUS_LABEL = {
    "expired": "EXPIRED",
    "missing": "MISSING",
    "logged_out": "NOT LOGGED IN",
    "checkpoint": "ACCOUNT LOCKED",
    "expiring": "expiring soon",
    "optional_missing": "none (not required)",
    "ok": "valid",
}


def _cookie_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Cookie status per platform.

    Answers "which cookie do I need to refresh?" at a glance: status,
    remaining days, and the number of requests that failed because of it.
    """
    config = context.application.bot_data["config"]
    cookie_log = context.application.bot_data.get("cookie_log")
    cf = config.cookies_file

    failures = cookie_log.failures() if cookie_log else {}
    rows = platform_cookie_status(cf, failures=failures)

    # The hourly watch knows something the file cannot: whether the platform
    # still recognises the session. Where it has an answer, it wins.
    watch = read_state(config.data_dir)
    watched = watch.get("platforms") or {}
    for row in rows:
        entry = watched.get(row["platform"])
        if not isinstance(entry, dict):
            continue
        live = entry.get("live_state")
        if live in {"logged_out", "checkpoint"}:
            row["status"] = live
            row["live_detail"] = str(entry.get("detail") or "")
        elif live == "ok" and row["status"] == "logged_out":
            row["status"] = "ok"

    lines = ["<b>🍪 Cookie Status</b>", ""]

    if not cf.exists():
        lines.append(f"❌ No cookie file:\n<code>{cf}</code>\n")
    else:
        try:
            size = cf.stat().st_size
        except OSError:
            size = 0
        from bot.utils import human_bytes
        total = sum(r["count"] for r in rows)
        lines.append(f"📄 <code>{cf.name}</code> · {human_bytes(size)} · {total} cookies")
        lines.append("")

    problems = [
        r for r in rows
        if r["status"] in {"expired", "missing", "logged_out", "expiring"}
    ]

    for row in rows:
        icon = _COOKIE_STATUS_ICON.get(row["status"], "⚪️")
        label = _COOKIE_STATUS_LABEL.get(row["status"], row["status"])
        parts = [f"{icon} <b>{row['platform']}</b> — {label}"]

        if row["count"]:
            detail = f"{row['count']} cookies"
            if row["days_left"] is not None:
                if row["days_left"] < 0:
                    detail += " · expired"
                elif row["days_left"] == 0:
                    detail += " · <b>expires today</b>"
                else:
                    detail += f" · {row['days_left']} days left"
            if row["expired"]:
                detail += f" · {row['expired']} already expired"
            parts.append(f"   <i>{detail}</i>")

        if row["status"] == "logged_out":
            parts.append(
                "   <i>"
                + (_esc(row.get("live_detail")) + " — " if row.get("live_detail") else
                   "cookies present but no session cookie — ")
                + "re-export from a logged-in browser</i>"
            )

        if row["status"] == "checkpoint":
            parts.append(
                "   <i>the account is waiting for verification — "
                "re-exporting the cookie alone will not fix it</i>"
            )

        if row["failures"]:
            reason = _esc(row["last_reason"])[:60]
            parts.append(
                f"   ⚠️ <b>{row['failures']}</b> failed requests"
                + (f" · last reason: {reason}" if reason else "")
            )

        lines.append("\n".join(parts))

    lines.append("")
    if problems:
        names = ", ".join(r["platform"] for r in problems)
        lines.append(f"👉 <b>Needs refreshing:</b> {names}")
    else:
        lines.append("✅ Every platform's cookies are valid.")

    checked = watch.get("checked")
    if checked:
        lines.append(
            f"\n🕒 <i>Last live check: "
            f"{time.strftime('%H:%M', time.localtime(checked))} — hourly. "
            f"A detailed report is sent once a day.</i>"
        )

    cooldowns = CookieCooldown(config.data_dir).entries()
    if cooldowns:
        names = ", ".join(
            f"{name} ({max(0, int(float(e.get('until', 0)) - time.time()) // 60)} min)"
            for name, e in cooldowns.items()
        )
        lines.append(
            f"🧊 <i>Rate limited, requests kept anonymous: {names}</i>"
        )

    total_fail = cookie_log.total() if cookie_log else 0
    if total_fail:
        lines.append(
            f"\n📁 <b>{total_fail}</b> total cookie related errors "
            f"— <code>logs/cookie_errors.log</code>"
        )

    return "\n".join(lines)


def _cookie_log_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Raw lines of the most recent cookie failures."""
    cookie_log = context.application.bot_data.get("cookie_log")
    if not cookie_log:
        return "<b>🍪 Cookie Log</b>\n\nNo records."

    entries = cookie_log.tail(12)
    if not entries:
        return (
            "<b>🍪 Cookie Log</b>\n\n"
            "No cookie related error has been recorded yet. ✅"
        )

    body = "\n\n".join(f"<code>{_esc(line)}</code>" for line in reversed(entries))
    return f"<b>🍪 Cookie Log</b> <i>(last {len(entries)})</i>\n\n{body}"


# ── Analytics dashboard ──────────────────────────────────────────────────────

def _sparkline(values: list[int]) -> str:
    """Small text chart of the daily download trend."""
    if not values or max(values) == 0:
        return "▁" * len(values)
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values)
    return "".join(blocks[min(len(blocks) - 1, int(v / peak * (len(blocks) - 1)))] for v in values)


def _bar(value: int, total: int, width: int = 10) -> str:
    if not total:
        return "░" * width
    filled = int(value / total * width)
    return "█" * filled + "░" * (width - filled)


def _analytics_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    if not db:
        return "<b>📈 Analytics</b>\n\nNo database."

    data = analytics.summary(db)
    daily = analytics.daily_counts(db, 7)
    platforms = analytics.platform_distribution(db, days=30)
    sources = analytics.source_distribution(db)
    split = data["chat_split"]
    fail = data["failure"]

    counts = [d["count"] for d in daily]
    week_total = sum(counts)

    lines = [
        "<b>📈 Analytics dashboard</b>",
        "",
        "<b>Active users</b>",
        f"  Today: <b>{data['dau']}</b> · Week: <b>{data['wau']}</b> · Month: <b>{data['mau']}</b>",
        "",
        "<b>Downloads</b>",
        f"  Today: <b>{data['downloads_today']}</b> · "
        f"7 days: <b>{week_total}</b> · Total: <b>{data['total_downloads']}</b>",
        f"  <code>{_sparkline(counts)}</code> <i>last 7 days</i>",
        "",
        "<b>Success rate (7 days)</b>",
        f"  <code>{_bar(fail['ok'], fail['total'])}</code> "
        f"<b>{fail['rate']:.0f}%</b> ({fail['ok']}/{fail['total']})",
        "",
        "<b>Chat split</b>",
        f"  👤 Private: <b>{split['private']}</b> · 👥 Group: <b>{split['group']}</b>",
    ]

    if platforms:
        total = sum(int(p["count"]) for p in platforms) or 1
        lines.append("")
        lines.append("<b>Platform distribution (30 days)</b>")
        for row in platforms[:6]:
            count = int(row["count"])
            lines.append(
                f"  {_esc(row['platform']):<14} <code>{_bar(count, total, 8)}</code> "
                f"<b>{count}</b> <i>({count * 100 // total}%)</i>"
            )

    if sources:
        parts = " · ".join(f"{_esc(s['source'])}: <b>{s['count']}</b>" for s in sources)
        lines.append("")
        lines.append(f"<b>Download source</b>\n  {parts}")

    buffer = context.application.bot_data.get("activity_buffer")
    if buffer and buffer.pending():
        lines.append(f"\n<i>({buffer.pending()} activity records waiting to be written)</i>")

    return "\n".join(lines)


def _top_users_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    if not db:
        return "<b>🏆 Most Active Users</b>\n\nNo database."

    rows = analytics.top_users(db, 15)
    if not rows:
        return "<b>🏆 Most Active Users</b>\n\nNo records yet."

    lines = ["<b>🏆 Most Active Users</b>", ""]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}

    for index, row in enumerate(rows):
        mark = medals.get(index, f"{index + 1}.")
        name = row.get("username")
        label = f"@{_esc(name)}" if name else _esc(row.get("first_name") or "—")
        last = row.get("last_activity")
        when = datetime.fromtimestamp(last).strftime("%d.%m") if last else "-"
        lines.append(
            f"{mark} {label} — <b>{row['total_downloads']}</b> downloads "
            f"<i>({when})</i>\n     <code>{row['user_id']}</code>"
        )

    return "\n".join(lines)


def _analytics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Most active", callback_data="admin|topusers"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin|analytics"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


# ── Broadcast ─────────────────────────────────────────────────────────────────

_BC_KIND_LABEL = {
    "all": "everyone (users + groups)",
    "users": "private chats only",
    "groups": "groups only",
}


def _bc_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """The admin's broadcast draft (single admin, so it's application-wide)."""
    return context.application.bot_data.setdefault("broadcast_compose", {})


def _bc_palette_text() -> str:
    """The :slot: shortcuts an announcement can use, with a live preview."""
    lines = [
        "🎨 <b>Emoji shortcuts</b>",
        "<i>Type these in the text and they become the emoji you bound in "
        "the emoji panel:</i>",
    ]
    lines += [
        f"{em(key)} <code>:{key}:</code>"
        for _, key, _, _ in broadcast_slots()
    ]
    return "\n".join(lines)


def _broadcast_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    draft = _bc_state(context)
    kind = draft.get("kind", "all")

    counts = {"all": 0, "users": 0, "groups": 0}
    if db:
        try:
            counts = {k: len(db.broadcast_targets(kind=k)) for k in counts}
        except Exception:
            logger.exception("Could not read broadcast targets")

    lines = [
        "<b>📣 Send a Broadcast</b>",
        "",
        f"🎯 Audience: <b>{_BC_KIND_LABEL.get(kind, kind)}</b>",
        f"👥 Reach: <b>{counts.get(kind, 0)}</b> chats",
        "",
        f"<i>Total: {counts['users']} private · {counts['groups']} groups "
        "(opted-out and blocked chats excluded)</i>",
        "",
    ]

    message = draft.get("text")
    if message:
        # Shown the way recipients will see it, not as escaped source: the
        # point of a preview is catching a broken tag before 5000 people do.
        preview, used = render_slots(message)
        problem = validate_html(preview)
        if len(preview) > 600:
            preview = preview[:600] + "…"
        lines.append("<b>📝 Message preview</b>")
        if problem:
            # Rendering broken HTML here would take the panel down with it.
            lines.append(f"<blockquote>{_esc(preview)}</blockquote>")
            lines.append(f"⚠️ <b>Telegram would reject this:</b> {problem}")
            lines.append("<i>Fix the text and send it again.</i>")
            return "\n".join(lines)
        lines.append(f"<blockquote>{preview}</blockquote>")
        if used:
            lines.append(
                "<i>Emoji slots used: "
                + ", ".join(f"<code>:{key}:</code>" for key in used)
                + "</i>"
            )
        lines.append("")
        lines.append("Ready to send. ⬇️")
    else:
        lines.append(
            "✍️ <b>No message yet.</b>\n"
            "<i>Tap the button below and send me the broadcast text. "
            "HTML formatting (bold, italic, links) and <code>:emoji_slot:</code> "
            "shortcuts are supported.</i>"
        )

    return "\n".join(lines)


def _broadcast_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    draft = _bc_state(context)
    kind = draft.get("kind", "all")
    text = draft.get("text") or ""
    has_text = bool(text) and not validate_html(render_slots(text)[0])

    kind_row = [
        InlineKeyboardButton(
            ("🔘 " if kind == k else "⚪️ ") + label,
            callback_data=f"admin|bckind|{k}",
        )
        for k, label in (("all", "Everyone"), ("users", "Private"), ("groups", "Groups"))
    ]

    rows = [kind_row]

    if has_text:
        rows.append([InlineKeyboardButton("🚀 Send", callback_data="admin|bcconfirm")])
        rows.append([
            InlineKeyboardButton("✏️ Edit message", callback_data="admin|bcwrite"),
            InlineKeyboardButton("🗑 Clear message", callback_data="admin|bcclear"),
        ])
    elif text:
        rows.append([
            InlineKeyboardButton("✏️ Rewrite message", callback_data="admin|bcwrite"),
            InlineKeyboardButton("🗑 Clear message", callback_data="admin|bcclear"),
        ])
    else:
        rows.append([InlineKeyboardButton("✍️ Write message", callback_data="admin|bcwrite")])

    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])
    return InlineKeyboardMarkup(rows)


# ── Log viewer ────────────────────────────────────────────────────────────────

_LOG_LEVEL_ICON = {
    "ERROR": "🔴",
    "CRITICAL": "🔴",
    "WARNING": "🟠",
    "INFO": "⚪️",
    "DEBUG": "⚫️",
}

_LOG_SOURCES = {
    "live": ("Live stream (memory)", None, "🔴 Live"),
    "bot": ("bot.log", "bot.log", "📄 Bot"),
    "downloads": ("downloads.log", "downloads.log", "📥 Downloads"),
    "cookie": ("cookie_errors.log", "cookie_errors.log", "🍪 Cookies"),
}


def _read_log_tail(path, lines: int = 25) -> list[str]:
    """Reads the last N lines of a file without loading it all into memory."""
    try:
        size = path.stat().st_size
    except OSError:
        return []

    chunk = min(size, 60_000)
    try:
        with path.open("rb") as fh:
            fh.seek(size - chunk)
            data = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    return [ln for ln in data.splitlines() if ln.strip()][-lines:]


def _logs_text(context: ContextTypes.DEFAULT_TYPE, source: str = "live", level: str = "all") -> str:
    config = context.application.bot_data["config"]
    label, filename, _short = _LOG_SOURCES.get(source, _LOG_SOURCES["live"])

    if filename:
        entries = _read_log_tail(config.log_dir / filename, 40)
    else:
        from bot.log_buffer import last_lines
        entries = last_lines(40)

    if level != "all":
        entries = [e for e in entries if f"| {level} " in e or f"| {level}|" in e]

    if not entries:
        if level != "all":
            return (
                f"<b>📜 Log — {label}</b>\n\n"
                f"<i>No <b>{level}</b> lines in this channel.</i> ✅\n\n"
                "Select «All» to see every line."
            )
        return (
            f"<b>📜 Log — {label}</b>\n\n"
            "<i>Nothing here yet.</i>"
        )

    entries = entries[-15:]
    body_parts = []
    for line in entries:
        icon = ""
        for name, symbol in _LOG_LEVEL_ICON.items():
            if f"| {name} " in line or f"| {name}|" in line:
                icon = symbol + " "
                break
        trimmed = line if len(line) <= 220 else line[:220] + "…"
        body_parts.append(f"{icon}<code>{_esc(trimmed)}</code>")

    filter_note = "" if level == "all" else f" · filter: <b>{level}</b>"
    return (
        f"<b>📜 Log — {label}</b> <i>(last {len(entries)}{filter_note})</i>\n\n"
        + "\n\n".join(body_parts)
    )


def _logs_keyboard(source: str = "live", level: str = "all") -> InlineKeyboardMarkup:
    source_row = [
        InlineKeyboardButton(
            ("• " if source == key else "") + short,
            callback_data=f"admin|logs|{key}|{level}",
        )
        for key, (_label, _file, short) in _LOG_SOURCES.items()
    ]

    level_row = [
        InlineKeyboardButton(
            ("• " if level == key else "") + name,
            callback_data=f"admin|logs|{source}|{key}",
        )
        for key, name in (("all", "All"), ("ERROR", "🔴 Errors"), ("WARNING", "🟠 Warnings"))
    ]

    return InlineKeyboardMarkup([
        source_row,
        level_row,
        [
            InlineKeyboardButton("📤 Download file", callback_data=f"admin|logfile|{source}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"admin|logs|{source}|{level}"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


# ── User search / profile / ban management ───────────────────────────────────

def _user_label(row: dict) -> str:
    name = row.get("username")
    if name:
        return f"@{_esc(name)}"
    return _esc(row.get("first_name") or f"ID {row.get('user_id')}")


def _search_results_text(context: ContextTypes.DEFAULT_TYPE, term: str) -> tuple[str, InlineKeyboardMarkup]:
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]

    users = db.search_users(term, limit=8) if db else []
    chats = db.search_chats(term, limit=5) if db else []

    lines = [f"<b>🔍 Search:</b> <code>{_esc(term)}</code>", ""]
    rows: list[list[InlineKeyboardButton]] = []

    if not users and not chats:
        lines.append(
            "No results.\n\n<i>Search by username, first name or a numeric ID.</i>"
        )
    else:
        if users:
            lines.append(f"<b>👤 Users ({len(users)})</b>")
            for row in users:
                uid = int(row["user_id"])
                banned = permissions.is_user_banned(uid)
                mark = "🚫" if banned else "✅"
                lines.append(
                    f"{mark} {_user_label(row)} — <b>{row.get('total_downloads', 0)}</b> downloads\n"
                    f"    <code>{uid}</code>"
                )
                rows.append([InlineKeyboardButton(
                    f"{mark} {_user_label(row)[:20]}", callback_data=f"admin|userinfo|{uid}"
                )])

        if chats:
            lines.append(f"\n<b>💬 Chats ({len(chats)})</b>")
            for row in chats:
                cid = int(row["chat_id"])
                banned = permissions.is_group_banned(cid)
                mark = "🚫" if banned else "✅"
                title = _esc(row.get("title") or "(untitled)")[:30]
                lines.append(f"{mark} <b>{title}</b> — <code>{cid}</code>")
                # Chats used to be listed with no button — banning a group
                # from the panel wasn't possible at all.
                rows.append([InlineKeyboardButton(
                    f"{mark} {title[:20]}", callback_data=f"admin|chatinfo|{cid}"
                )])

    rows.append([
        InlineKeyboardButton("🔍 New search", callback_data="admin|usersearch"),
        InlineKeyboardButton("‹ Bans", callback_data="admin|bans"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _user_info_text(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]
    live_guard = context.application.bot_data.get("live_guard")

    row = db.get_user(user_id) if db else None
    banned = permissions.is_user_banned(user_id)

    if not row:
        lines = [
            f"<b>👤 User</b> <code>{user_id}</code>",
            "",
            "<i>No record in the database (may have never used the bot).</i>",
            f"\nBan status: {'🚫 <b>Banned</b>' if banned else '✅ Not banned'}",
        ]
    else:
        first = datetime.fromtimestamp(row["first_seen"]).strftime("%d.%m.%Y")
        last = datetime.fromtimestamp(row["last_activity"]).strftime("%d.%m.%Y %H:%M")
        lines = [
            f"<b>👤 {_user_label(row)}</b>",
            f"<code>{user_id}</code>",
            "",
            f"📥 Total downloads: <b>{row['total_downloads']}</b>",
            f"📅 First seen: <b>{first}</b>",
            f"🕐 Last active: <b>{last}</b>",
            f"🔔 Broadcasts: <b>{'off' if row['broadcast_opt_out'] else 'on'}</b>",
            f"🚷 Unreachable: <b>{'yes' if row['is_blocked'] else 'no'}</b>",
            f"\nBan status: {'🚫 <b>Banned</b>' if banned else '✅ Not banned'}",
        ]

        if live_guard:
            remaining = live_guard.ban_remaining(user_id)
            if remaining > 0:
                lines.append(
                    f"⏳ Livestream ban: <b>{format_duration(remaining)}</b> left"
                )

        recent = db.user_downloads(user_id, 5) if db else []
        if recent:
            lines.append("\n<b>Recent downloads</b>")
            for item in recent:
                when = datetime.fromtimestamp(item["created_at"]).strftime("%d.%m %H:%M")
                icon = "✅" if item["result"] == "success" else "❌"
                lines.append(f"  {icon} {_esc(item['platform'] or '—')} · <i>{when}</i>")

    action = (
        InlineKeyboardButton("✅ Remove ban", callback_data=f"admin|unban|{user_id}")
        if banned else
        InlineKeyboardButton("🚫 Ban", callback_data=f"admin|banask|{user_id}")
    )

    rows = [[action]]
    if live_guard and live_guard.ban_remaining(user_id) > 0:
        rows.append([InlineKeyboardButton(
            "⏳ Clear livestream ban", callback_data=f"admin|livewipe|{user_id}"
        )])
    rows.append([
        InlineKeyboardButton("🔍 Search", callback_data="admin|usersearch"),
        InlineKeyboardButton("‹ Bans", callback_data="admin|bans"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _chat_info_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Group/channel profile — the chat equivalent of the user profile."""
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    rows_db = db.search_chats(str(chat_id), limit=1) if db else []
    row = rows_db[0] if rows_db else None
    banned = permissions.is_group_banned(chat_id)
    active = len([
        j for j in manager.jobs.values()
        if j.chat_id == chat_id and not j.done and not j.cancelled
    ])

    if not row:
        lines = [
            f"<b>💬 Chat</b> <code>{chat_id}</code>",
            "",
            "<i>No record in the database.</i>",
        ]
    else:
        first = datetime.fromtimestamp(row["first_seen"]).strftime("%d.%m.%Y")
        last = datetime.fromtimestamp(row["last_activity"]).strftime("%d.%m.%Y %H:%M")
        lines = [
            f"<b>💬 {_esc(row.get('title') or '(untitled)')}</b>",
            f"<code>{chat_id}</code> · {_esc(row.get('chat_type') or '—')}",
            "",
            f"📥 Total downloads: <b>{row['total_downloads']}</b>",
            f"📅 First seen: <b>{first}</b>",
            f"🕐 Last active: <b>{last}</b>",
            f"🔔 Broadcasts: <b>{'off' if row['broadcast_opt_out'] else 'on'}</b>",
        ]

    lines.append(f"\nBan status: {'🚫 <b>Banned</b>' if banned else '✅ Not banned'}")
    if active:
        lines.append(f"⚙️ Active downloads: <b>{active}</b>")

    action = (
        InlineKeyboardButton("✅ Remove ban", callback_data=f"admin|unban|{chat_id}")
        if banned else
        InlineKeyboardButton("🚫 Ban group", callback_data=f"admin|banask|{chat_id}")
    )
    rows = [
        [action],
        [
            InlineKeyboardButton("🔍 Search", callback_data="admin|usersearch"),
            InlineKeyboardButton("‹ Bans", callback_data="admin|bans"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Ban list ──
def _bans_screen(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    permissions = context.application.bot_data["permissions"]
    db = context.application.bot_data.get("db")
    bans = permissions.list_bans()
    users = bans.get("users", [])
    groups = bans.get("groups", [])

    lines = ["<b>🚫 Ban Management</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []

    lines.append(f"<b>Users ({len(users)})</b>")
    if not users:
        lines.append("  —")
    for uid in users[:8]:
        row = db.get_user(uid) if db else None
        label = _user_label(row) if row else f"ID {uid}"
        lines.append(f"  • {label} — <code>{uid}</code>")
        # Clicking a banned entry was previously impossible; /unbanid had to
        # be typed by hand.
        rows.append([InlineKeyboardButton(
            f"👤 {label[:24]}", callback_data=f"admin|userinfo|{uid}"
        )])
    if len(users) > 8:
        lines.append(f"  …+{len(users) - 8}")

    lines.append(f"\n<b>Groups ({len(groups)})</b>")
    if not groups:
        lines.append("  —")
    for cid in groups[:8]:
        found = db.search_chats(str(cid), limit=1) if db else []
        title = _esc(found[0].get("title") or "(untitled)") if found else f"ID {cid}"
        lines.append(f"  • {title} — <code>{cid}</code>")
        rows.append([InlineKeyboardButton(
            f"💬 {title[:24]}", callback_data=f"admin|chatinfo|{cid}"
        )])
    if len(groups) > 8:
        lines.append(f"  …+{len(groups) - 8}")

    lines.append(
        "\n<i>Commands:</i> <code>/banid ID</code> · <code>/unbanid ID</code>\n"
        "<i>A negative ID bans a group, a positive ID bans a user.</i>"
    )

    rows.append([InlineKeyboardButton("🔍 Search users / groups", callback_data="admin|usersearch")])
    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── System status (same info as status_command, inside the panel) ──
def _system_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.application.bot_data["config"]
    manager = context.application.bot_data["process_manager"]
    active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
    ffmpeg = "✅" if shutil.which("ffmpeg") else "❌"
    gallery = "✅" if shutil.which("gallery-dl") else "❌"
    return (
        "<b>🖥 System Status</b>\n\n"
        f"Local Bot API: <b>{'on' if config.local_bot_api_base else 'off'}</b>\n"
        f"Active downloads: <b>{active}</b> / {config.max_simultaneous_downloads}\n"
        f"Max file size: <b>{config.max_file_size_mb} MB</b>\n\n"
        f"Python: <code>{sys.version.split()[0]}</code>\n"
        f"yt-dlp: <code>{yt_dlp.version.__version__}</code>\n"
        f"ffmpeg: {ffmpeg} | gallery-dl: {gallery}"
    )


# ── Usage list (paginated) ──
def _usage_text(context: ContextTypes.DEFAULT_TYPE, page: int, page_size: int = 8) -> tuple[str, InlineKeyboardMarkup]:
    import html as _html

    chats = context.application.bot_data["chat_registry"].all_chats()
    total = len(chats)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = chats[start:start + page_size]

    lines = [f"<b>💬 Bot Usage</b> — {total} chats"]
    if not chunk:
        lines.append("\nNo records yet.")
    for c in chunk:
        last = c.get("last_activity")
        when = datetime.fromtimestamp(last).strftime("%d.%m.%Y %H:%M") if last else "-"
        ctype = {"private": "private", "group": "group", "supergroup": "group", "channel": "channel"}.get(c.get("type", ""), c.get("type", "-"))
        title = _html.escape(str(c.get("title") or "(untitled)"))[:38]
        lines.append(
            f"\n• <b>{title}</b> <i>({ctype})</i>\n"
            f"  <code>{c.get('chat_id')}</code> · downloads: <b>{c.get('total_downloads', 0)}</b> · {when}"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"admin|usage|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="admin|noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"admin|usage|{page+1}"))

    rows = [nav, [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts sending the broadcast and shows live progress in the panel."""
    query = update.callback_query
    app = context.application
    draft = _bc_state(context)
    db = app.bot_data.get("db")

    text = draft.get("text")
    kind = draft.get("kind", "all")

    if not text or not db:
        await query.answer("No message to send.", show_alert=True)
        return

    # Only one broadcast at a time — a second tap should not start another.
    running = app.bot_data.get("broadcast_job")
    if running and running.running:
        await query.answer("A broadcast is already in progress.", show_alert=True)
        return

    targets = await asyncio.to_thread(db.broadcast_targets, kind=kind)
    # :slot: shortcuts become emoji here, once, rather than per recipient.
    rendered, _used = render_slots(text)
    problem = validate_html(rendered)
    if problem:
        await query.answer("The message's HTML is broken — fix it first.", show_alert=True)
        return

    job = BroadcastJob(text=rendered, targets=targets, kind=kind)
    app.bot_data["broadcast_job"] = job

    await query.answer("Sending started.")

    stop_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Stop", callback_data="admin|bcstop"),
    ]])
    await _edit(query, job.progress_text(), stop_markup)

    async def on_progress(current: BroadcastJob) -> None:
        markup = stop_markup if current.running else _back_keyboard()
        await _edit(query, current.progress_text(), markup)

    async def runner() -> None:
        try:
            await run_broadcast(app, job, db=db, on_progress=on_progress)
        except Exception:
            logger.exception("Broadcast delivery crashed")
        finally:
            # Clear the draft so it can't be sent twice by mistake.
            draft.pop("text", None)
            draft["awaiting"] = False
            try:
                await _edit(
                    query,
                    job.summary_text(),
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("📣 New broadcast", callback_data="admin|broadcast"),
                        InlineKeyboardButton("‹ Panel", callback_data="admin|panel"),
                    ]]),
                )
            except Exception:
                logger.exception("Could not show the broadcast summary")

    asyncio.create_task(runner())


async def broadcast_compose_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches the broadcast text the admin sends after tapping "Write message".

    The admin's first message in a PRIVATE chat after that tap becomes the
    draft. This handler runs before the link handler, so it never gets
    mistaken for a download request.
    """
    if not update.effective_user or not update.message:
        return

    if not _admin_ok(update, context):
        return

    if update.effective_chat and update.effective_chat.type != "private":
        return

    draft = _bc_state(context)

    # User search mode uses the same "awaiting input" mechanism as broadcast writing.
    if draft.get("awaiting_search"):
        draft["awaiting_search"] = False
        term = (update.message.text or "").strip()
        try:
            await update.message.delete()
        except Exception:
            pass

        text, markup = _search_results_text(context, term)
        chat_id = draft.get("search_chat_id")
        message_id = draft.get("search_message_id")
        if chat_id and message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text,
                    parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                await update.effective_chat.send_message(
                    text, parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
        raise ApplicationHandlerStop

    if not draft.get("awaiting"):
        return

    text = update.message.text_html or update.message.text or ""
    if not text.strip():
        return

    draft["text"] = text
    draft["awaiting"] = False

    # Got the draft; update the panel message and delete the admin's message
    # (keep the chat clean, don't leave the broadcast text lying around).
    try:
        await update.message.delete()
    except Exception:
        pass

    chat_id = draft.get("panel_chat_id")
    message_id = draft.get("panel_message_id")
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_broadcast_text(context),
                parse_mode="HTML",
                reply_markup=_broadcast_keyboard(context),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning("Broadcast panel could not be updated, sending a new message")
            await update.effective_chat.send_message(
                _broadcast_text(context),
                parse_mode="HTML",
                reply_markup=_broadcast_keyboard(context),
                disable_web_page_preview=True,
            )

    raise ApplicationHandlerStop


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return
    state = context.application.bot_data["bot_state"]
    await safe_reply(update.message,
        _panel_text(context),
        parse_mode="HTML",
        reply_markup=_panel_keyboard(state),
        disable_web_page_preview=True,
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles every admin| panel callback."""
    query = update.callback_query
    if not query:
        return

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(query.from_user.id if query.from_user else None):
        await query.answer("Admins only.", show_alert=True)
        return

    parts = (query.data or "").split("|")
    sub = parts[1] if len(parts) > 1 else ""
    state = context.application.bot_data["bot_state"]
    manager = context.application.bot_data["process_manager"]

    if sub == "noop":
        await query.answer()
        return

    if sub == "panel":
        await query.answer()
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "mode" and len(parts) >= 3:
        new_mode = state.set_mode(parts[2])  # logged inside state.py
        await query.answer(f"Mode: {_MODE_LABEL.get(new_mode, new_mode)}")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "toggle":
        enabled = not state.get_enabled()
        state.set_enabled(enabled)
        if not enabled:
            manager.shutdown()
        await query.answer("Bot started." if enabled else "Bot stopped.")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "langmenu":
        await query.answer()
        await _edit(query, "<b>🌐 Bot language</b>\n\nLanguage of user-facing messages:", _language_keyboard(state.get_language()))
        return

    if sub == "lang" and len(parts) >= 3:
        code = parts[2]
        if code in LANGUAGES:
            state.set_language(code)   # persisted + logged
            set_language(code)         # applied immediately
            await query.answer(f"Language: {LANGUAGES[code]}")
        else:
            await query.answer()
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    # OLD CALLBACK: the "📊 Stats" button was replaced by "📈 Analytics".
    # Still clickable from old panel messages in chat history; route it to
    # the new screen instead of showing a dead one.
    if sub == "stats":
        await query.answer()
        await _edit(query, _analytics_text(context), _analytics_keyboard())
        return

    if sub == "status":
        await query.answer()
        await _edit(query, _system_text(context), _back_keyboard())
        return

    if sub == "cookie":
        await query.answer()
        await _edit(query, _cookie_text(context), _cookie_keyboard())
        return

    if sub == "cookielog":
        await query.answer()
        await _edit(query, _cookie_log_text(context), _cookie_log_keyboard())
        return

    if sub == "cookiereset":
        cookie_log = context.application.bot_data.get("cookie_log")
        if cookie_log:
            cookie_log.reset()
        await query.answer("Cookie error counters reset.")
        await _edit(query, _cookie_text(context), _cookie_keyboard())
        return

    if sub == "bans":
        await query.answer()
        text, markup = _bans_screen(context)
        await _edit(query, text, markup)
        return

    if sub == "logs":
        source = parts[2] if len(parts) > 2 else "live"
        level = parts[3] if len(parts) > 3 else "all"
        if source not in _LOG_SOURCES:
            source = "live"
        await query.answer()
        await _edit(query, _logs_text(context, source, level), _logs_keyboard(source, level))
        return

    if sub == "logfile" and len(parts) >= 3:
        source = parts[2]
        label, filename, _short = _LOG_SOURCES.get(source, (None, None, None))
        if not filename:
            await query.answer("This channel lives in memory only, no file.", show_alert=True)
            return

        path = context.application.bot_data["config"].log_dir / filename
        if not path.exists() or path.stat().st_size == 0:
            await query.answer("File is empty or missing.", show_alert=True)
            return

        await query.answer("Sending...")
        try:
            with path.open("rb") as fh:
                await query.message.reply_document(document=fh, filename=filename)
        except Exception as exc:
            logger.warning("Could not send the log file: %s", exc)
            await query.answer("Could not send the file.", show_alert=True)
        return

    if sub == "usersearch":
        draft = _bc_state(context)
        draft["awaiting_search"] = True
        draft["search_chat_id"] = query.message.chat_id if query.message else None
        draft["search_message_id"] = query.message.message_id if query.message else None
        await query.answer()
        await _edit(
            query,
            "🔍 <b>Search a user / chat</b>\n\n"
            "Send the text to search for:\n"
            "• username (<code>@arif</code> or <code>arif</code>)\n"
            "• numeric ID (<code>8419768278</code>)\n"
            "• group title",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("‹ Cancel", callback_data="admin|bans"),
            ]]),
        )
        return

    if sub == "userinfo" and len(parts) >= 3:
        try:
            uid = int(parts[2])
        except ValueError:
            await query.answer("Invalid ID.", show_alert=True)
            return
        text, markup = _user_info_text(context, uid)
        await query.answer()
        await _edit(query, text, markup)
        return

    if sub == "chatinfo" and len(parts) >= 3:
        try:
            cid = int(parts[2])
        except ValueError:
            await query.answer("Invalid ID.", show_alert=True)
            return
        text, markup = _chat_info_text(context, cid)
        await query.answer()
        await _edit(query, text, markup)
        return

    if sub == "banask" and len(parts) >= 3:
        # A reversible but consequential action: still ask for confirmation.
        try:
            tid = int(parts[2])
        except ValueError:
            await query.answer("Invalid ID.", show_alert=True)
            return

        permissions = context.application.bot_data["permissions"]
        is_group = permissions.is_group_id(tid)
        back = f"admin|{'chatinfo' if is_group else 'userinfo'}|{tid}"
        detail = (
            "The bot won't respond in a banned group at all; any download "
            "running there right now is cancelled."
            if is_group else
            "A banned user can't use the bot at all, and their active "
            "download is cancelled."
        )

        await query.answer()
        await _edit(
            query,
            f"🚫 <b>Ban this {'group' if is_group else 'user'}?</b>\n\n"
            f"<code>{tid}</code>\n\n"
            f"<i>{detail} You can undo this at any time.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, ban", callback_data=f"admin|ban|{tid}"),
                InlineKeyboardButton("‹ Cancel", callback_data=back),
            ]]),
        )
        return

    if sub in {"ban", "unban"} and len(parts) >= 3:
        try:
            tid = int(parts[2])
        except ValueError:
            await query.answer("Invalid ID.", show_alert=True)
            return

        permissions = context.application.bot_data["permissions"]

        # The id's sign decides the target; the panel used to treat every id
        # as a user.
        if sub == "ban":
            is_group = permissions.ban_id(tid)
            if is_group:
                cancelled = manager.cancel_chat_jobs(tid)
                await query.answer(
                    "Group banned." + (f" {cancelled} downloads cancelled." if cancelled else "")
                )
            else:
                manager.cancel_user_job(tid)
                await query.answer(f"{tid} banned.")
        else:
            is_group = permissions.unban_id(tid)
            await query.answer(f"{tid} unbanned.")

        if is_group:
            text, markup = _chat_info_text(context, tid)
        else:
            text, markup = _user_info_text(context, tid)
        await _edit(query, text, markup)
        return

    if sub == "livewipe" and len(parts) >= 3:
        try:
            uid = int(parts[2])
        except ValueError:
            await query.answer("Invalid ID.", show_alert=True)
            return
        guard = context.application.bot_data.get("live_guard")
        if guard:
            guard.clear(uid)
        await query.answer("Livestream ban cleared.")
        text, markup = _user_info_text(context, uid)
        await _edit(query, text, markup)
        return

    if sub == "analytics":
        await query.answer()
        await _edit(query, _analytics_text(context), _analytics_keyboard())
        return

    if sub == "topusers":
        await query.answer()
        await _edit(query, _top_users_text(context), InlineKeyboardMarkup([[
            InlineKeyboardButton("‹ Analytics", callback_data="admin|analytics"),
        ]]))
        return

    # ── Broadcast ────────────────────────────────────────────────────────────
    if sub == "broadcast":
        await query.answer()
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bckind" and len(parts) >= 3:
        if parts[2] in {"all", "users", "groups"}:
            _bc_state(context)["kind"] = parts[2]
        await query.answer()
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcwrite":
        draft = _bc_state(context)
        draft["awaiting"] = True
        draft["panel_chat_id"] = query.message.chat_id if query.message else None
        draft["panel_message_id"] = query.message.message_id if query.message else None
        await query.answer()
        await _edit(
            query,
            "✍️ <b>Send the broadcast text</b>\n\n"
            "Write the announcement now — your next message will be taken "
            "as the draft.\n\n"
            "<i>HTML is supported: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, "
            "&lt;a href=\"...\"&gt;link&lt;/a&gt;</i>\n\n"
            + _bc_palette_text(),
            InlineKeyboardMarkup([[
                InlineKeyboardButton("‹ Cancel", callback_data="admin|bccancelwrite"),
            ]]),
        )
        return

    if sub == "bccancelwrite":
        _bc_state(context)["awaiting"] = False
        await query.answer("Cancelled.")
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcclear":
        draft = _bc_state(context)
        draft.pop("text", None)
        draft["awaiting"] = False
        await query.answer("Message cleared.")
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcconfirm":
        draft = _bc_state(context)
        if not draft.get("text"):
            await query.answer("Write a message first.", show_alert=True)
            return

        db = context.application.bot_data.get("db")
        kind = draft.get("kind", "all")
        count = len(db.broadcast_targets(kind=kind)) if db else 0

        if not count:
            await query.answer("Nobody in this audience.", show_alert=True)
            return

        final, _used = render_slots(draft["text"])
        problem = validate_html(final)
        if problem:
            await query.answer("The message's HTML is broken — fix it first.", show_alert=True)
            return

        if len(final) > 500:
            final = final[:500] + "…"

        await query.answer()
        await _edit(
            query,
            f"🚀 <b>Send this broadcast?</b>\n\n"
            f"🎯 Audience: <b>{_BC_KIND_LABEL.get(kind, kind)}</b>\n"
            f"👥 Recipients: <b>{count}</b> chats\n"
            f"⏱ Estimated time: <b>~{max(1, count // 20)} seconds</b>\n\n"
            f"<blockquote>{final}</blockquote>\n"
            "<i>This is exactly what will be delivered. "
            "You can stop it once it starts.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, send", callback_data="admin|bcsend"),
                InlineKeyboardButton("‹ Cancel", callback_data="admin|broadcast"),
            ]]),
        )
        return

    if sub == "bcsend":
        await _start_broadcast(update, context)
        return

    if sub == "bcstop":
        job = context.application.bot_data.get("broadcast_job")
        if job and job.running:
            job.cancelled = True
            await query.answer("Stopping...")
        else:
            await query.answer("Nothing is running.")
        return

    if sub == "close":
        await query.answer("Panel closed.")
        try:
            await query.message.delete()
        except Exception:
            await _edit(query, "<b>⚙️ Panel closed.</b> Reopen with /admin.",
                        InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="admin|panel")]]))
        return

    if sub == "usage" and len(parts) >= 3:
        page = int(parts[2]) if parts[2].isdigit() else 0
        text, markup = _usage_text(context, page)
        await query.answer()
        await _edit(query, text, markup)
        return

    # Irreversible action: ask for confirmation first. It used to cancel
    # every active download on a single tap with no way back.
    if sub == "clear":
        active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
        pending = len(context.application.bot_data.get("pending_jobs") or {})
        await query.answer()
        await _edit(
            query,
            "🧹 <b>Clear active jobs?</b>\n\n"
            f"• Downloads in progress: <b>{active}</b>\n"
            f"• Open format menus: <b>{pending}</b>\n\n"
            "<i>This can't be undone; users' downloads in progress will be cancelled.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, clear", callback_data="admin|clear_yes"),
                InlineKeyboardButton("‹ Cancel", callback_data="admin|panel"),
            ]]),
        )
        return

    if sub == "clear_yes":
        manager.shutdown()
        cleared = await clear_all_pending(context.application)
        context.application.bot_data["playlist_sessions"] = {}
        await query.answer(f"Cleared. ({cleared} menus removed)")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    await query.answer()
