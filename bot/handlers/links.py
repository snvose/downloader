from __future__ import annotations

"""
Handles incoming media links: the YouTube playlist browser, the YouTube
format menu, YouTube Music/Spotify audio downloads, and direct downloads for
every other platform.
"""

import asyncio
import html
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.safe_message import safe_message_edit, safe_reply
from bot.emoji_manager import em
from bot.i18n import t
from bot.live_guard import format_duration, guard_message, info_is_live, probe_is_live
from bot.pending import clear_user_pending
from bot.ui import analyzing_text, unsupported_spotify_text
from bot.utils import (
    extract_first_url,
    instagram_story_kind,
    is_profile_url,
    is_spotify_url,
    is_supported_url,
    normalize_url,
    platform_name,
)

logger = logging.getLogger("downloader")

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── HTML escaping ──────────────────────────────────────────────────────────

def _esc(v: object) -> str:
    return html.escape(str(v or ""))


# ─── Platform / URL detection ───────────────────────────────────────────────

def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(x in host for x in ["youtube.com", "youtu.be", "music.youtube"])


def _is_youtube_music_url(url: str) -> bool:
    return "music.youtube" in (urlparse(url).netloc or "").lower()


def _is_playlist_url(url: str) -> bool:
    """Is this a YouTube playlist URL (not a single video)?"""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if not any(x in host for x in ["youtube.com", "youtu.be"]):
            return False
        qs = parse_qs(parsed.query)
        if "/playlist" in parsed.path.lower():
            return True
        # list= is present but this isn't a single video
        return bool(qs.get("list")) and not bool(qs.get("v"))
    except Exception:
        return False


def _human_dur(secs: int | None) -> str:
    if not secs:
        return t("unknown")
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ─── Synchronous metadata fetching (runs in an executor) ────────────────────

def _sync_extract_info(url: str, cookies_file: Path | None = None) -> dict:
    """
    Fetches metadata without downloading (process=True, needed for
    track/artist/album). Tries with cookies, then without.
    """
    plat = platform_name(url)
    noplaylist = plat not in {"Instagram", "TikTok", "Reddit", "Pinterest"}
    # A link to one story is a link to one story, not to the user's whole tray.
    if instagram_story_kind(url) == "single":
        noplaylist = True

    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": noplaylist,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": _HTTP_HEADERS.copy(),
    }

    attempts = [True, False]  # with cookies / without
    last_exc: Exception | None = None

    for use_cookies in attempts:
        opts = dict(base_opts)
        if use_cookies and cookies_file and cookies_file.exists():
            opts["cookiefile"] = str(cookies_file)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=True)
            if info is None:
                raise RuntimeError("Could not fetch content info.")
            return _compact_metadata(info, url)
        except Exception as exc:
            last_exc = exc

    raise last_exc or RuntimeError("Could not fetch content info.")


def _sync_extract_playlist(url: str, cookies_file: Path | None = None) -> dict:
    """Fetches playlist info quickly (process=False, extract_flat). Returns raw yt-dlp info."""
    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": _HTTP_HEADERS.copy(),
    }

    last_exc: Exception | None = None
    for use_cookies in [True, False]:
        opts = dict(base_opts)
        if use_cookies and cookies_file and cookies_file.exists():
            opts["cookiefile"] = str(cookies_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            if info is None:
                raise RuntimeError("Could not fetch playlist info.")
            return info  # type: ignore[return-value]
        except Exception as exc:
            last_exc = exc

    raise last_exc or RuntimeError("Could not fetch playlist info.")


def _compact_metadata(info: dict, url: str) -> dict:
    """Turns the raw yt-dlp info dict into a small dict."""
    if not isinstance(info, dict):
        return {"platform": platform_name(url), "webpage_url": url, "title": ""}

    webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)

    plat = platform_name(webpage_url)
    if plat == "YouTube":
        extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
        if "music.youtube" in webpage_url:
            plat = "YouTube Music"
        elif "youtube" in extractor and (
            info.get("track") or info.get("artist") or info.get("album")
        ):
            plat = "YouTube Music"

    description = info.get("description") or info.get("alt_title") or ""
    if isinstance(description, list):
        description = "\n".join(str(x) for x in description if x)

    artist = info.get("artist") or ""
    if isinstance(artist, list):
        artist = ", ".join(str(x) for x in artist if x)

    return {
        "platform": plat,
        # Livestream flag: metadata is already fetched, so this is free.
        "is_live": info_is_live(info),
        "title": str(info.get("title") or info.get("fulltitle") or ""),
        "track": str(info.get("track") or ""),
        "artist": str(artist),
        "album": str(info.get("album") or ""),
        "release_year": info.get("release_year"),
        "description": str(description),
        "uploader": str(
            info.get("uploader") or info.get("channel") or info.get("creator") or ""
        ),
        "uploader_id": str(info.get("uploader_id") or info.get("channel_id") or ""),
        "duration": info.get("duration"),
        "webpage_url": webpage_url,
        "thumbnail": str(info.get("thumbnail") or ""),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
    }


# ─── YouTube UI ───────────────────────────────────────────────────────────────

def build_youtube_action_keyboard(job_id: str, view: str = "main") -> InlineKeyboardMarkup:
    """YouTube format selection keyboard. view: main | video | audio"""
    back = InlineKeyboardButton("‹ Back", callback_data=f"menujob|{job_id}|main")

    if view == "video":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✨ Best",  callback_data=f"do|{job_id}|video_best"),
                InlineKeyboardButton("1080p",    callback_data=f"do|{job_id}|video_1080"),
            ],
            [
                InlineKeyboardButton("720p",     callback_data=f"do|{job_id}|video_720"),
                InlineKeyboardButton("480p",     callback_data=f"do|{job_id}|video_480"),
                InlineKeyboardButton("360p",     callback_data=f"do|{job_id}|video_360"),
            ],
            # Subtitles are burned into the video (no separate .srt file).
            [InlineKeyboardButton("🔤 Subtitled (TR)", callback_data=f"do|{job_id}|video_720|tr")],
            [InlineKeyboardButton("🔤 Subtitled (EN)", callback_data=f"do|{job_id}|video_720|en")],
            [back],
        ])

    if view == "audio":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✨ Best", callback_data=f"do|{job_id}|audio_best"),
                InlineKeyboardButton("320k",    callback_data=f"do|{job_id}|audio_320"),
            ],
            [
                InlineKeyboardButton("192k",    callback_data=f"do|{job_id}|audio_192"),
                InlineKeyboardButton("128k",    callback_data=f"do|{job_id}|audio_128"),
            ],
            # FLAC is lossless but won't improve quality if the source is
            # already lossy; it's a separate option, not the default.
            [InlineKeyboardButton("🎼 FLAC (lossless)", callback_data=f"do|{job_id}|audio_flac")],
            [back],
        ])

    # main view
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"menujob|{job_id}|video"),
            InlineKeyboardButton("🎧 Audio", callback_data=f"menujob|{job_id}|audio"),
        ],
        [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"do|{job_id}|thumbnail")],
    ])


def build_youtube_info_caption(info: dict, job_id: str) -> str:
    """YouTube / YouTube Music info screen text."""
    plat = info.get("platform", "YouTube")

    if plat == "YouTube Music":
        track  = info.get("track")  or info.get("title") or t("unknown")
        artist = info.get("artist") or info.get("uploader") or t("unknown")
        album  = info.get("album")  or "—"
        year   = info.get("release_year") or "—"
        dur    = _human_dur(info.get("duration"))
        return (
            f"🎵 <b>YouTube Music</b>\n"
            f"┌ 🎼 <b>{_esc(str(track)[:160])}</b>\n"
            f"├ 👤 {_esc(str(artist)[:100])}\n"
            f"├ 💿 {_esc(str(album)[:100])}\n"
            f"├ 📅 {_esc(str(year))}\n"
            f"└ ⏱ {_esc(dur)}"
        )

    # YouTube
    title    = info.get("title")    or t("unknown")
    uploader = info.get("uploader") or info.get("channel") or t("unknown")
    dur      = _human_dur(info.get("duration"))
    lines = [
        "▶️ <b>YouTube</b>",
        f"┌ 🎬 <b>{_esc(str(title)[:180])}</b>",
        f"├ 👤 {_esc(str(uploader)[:100])}",
        f"├ ⏱ {_esc(dur)}",
    ]
    if info.get("view_count"):
        lines.append(f"├ 👁 {int(info['view_count']):,} views")
    if info.get("like_count"):
        lines.append(f"├ 👍 {int(info['like_count']):,}")

    desc = str(info.get("description") or "").strip()
    if desc:
        short = html.escape(desc[:800])
        lines.append(f"├ 📝 <blockquote expandable>{short}</blockquote>")

    lines.append("└ 📥 <i>Choose a format</i>")
    return "\n".join(lines)


# ─── Playlist UI ──────────────────────────────────────────────────────────────

def _build_playlist_type_keyboard(pjob_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"pl_type|{pjob_id}|video"),
            InlineKeyboardButton("🎵 Audio", callback_data=f"pl_type|{pjob_id}|audio"),
        ],
    ])


def build_playlist_track_keyboard(pjob: dict, pjob_id: str, page_size: int = 8) -> InlineKeyboardMarkup:
    entries  = pjob["entries"]
    page     = pjob.get("page", 0)
    selected = pjob.get("selected", set())
    mode     = pjob.get("type", "audio")
    total    = len(entries)
    pages    = max(1, (total + page_size - 1) // page_size)
    start    = page * page_size
    end      = min(start + page_size, total)

    rows: list[list[InlineKeyboardButton]] = []

    for i in range(start, end):
        e   = entries[i]
        raw = str(e.get("title") or e.get("id") or f"#{i+1}")
        lbl = f"{i+1}. {raw[:40]}"
        dur = e.get("duration")
        if dur:
            lbl += f" {_human_dur(int(dur))}"
        chk = "✅" if i in selected else "⬜"
        rows.append([
            InlineKeyboardButton(lbl, callback_data=f"pl_item|{pjob_id}|{i}"),
            InlineKeyboardButton(chk, callback_data=f"pl_sel|{pjob_id}|{i}"),
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pl_page|{pjob_id}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"pl_noop|{pjob_id}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pl_page|{pjob_id}|{page+1}"))
    rows.append(nav)

    action: list[InlineKeyboardButton] = []
    if selected:
        action.append(InlineKeyboardButton(
            f"⬇️ Download selected ({len(selected)})",
            callback_data=f"pl_dlsel|{pjob_id}",
        ))
    action.append(InlineKeyboardButton("⏩ Download all", callback_data=f"pl_dlall|{pjob_id}"))
    rows.append(action)

    switch_lbl = "🎬 Switch to video" if mode == "audio" else "🎵 Switch to audio"
    switch_typ = "video"              if mode == "audio" else "audio"
    rows.append([InlineKeyboardButton(switch_lbl, callback_data=f"pl_type|{pjob_id}|{switch_typ}")])
    rows.append([InlineKeyboardButton("🛑 Cancel", callback_data=f"pl_cancel|{pjob_id}")])

    return InlineKeyboardMarkup(rows)


def build_playlist_header(pjob: dict) -> str:
    info  = pjob["info"]
    plat  = pjob.get("platform", "YouTube")
    title = str(info.get("title") or info.get("playlist_title") or "Playlist")
    upl   = str(info.get("uploader") or info.get("channel") or "")
    count = len(pjob["entries"])
    icon  = "🎵" if plat == "YouTube Music" else "📁"
    typ   = pjob.get("type")

    lines = [f"{icon} <b>{_esc(title[:80])}</b>"]
    if upl:
        lines.append(f"• <b>Channel:</b> {_esc(upl[:60])}")
    lines.append(f"• <b>Total:</b> {count} items")
    if typ:
        lines.append(f"• <b>Mode:</b> {'🎬 Video' if typ == 'video' else '🎵 Audio'}")

    sel = pjob.get("selected", set())
    if sel:
        lines.append(f"✔️ <i>{len(sel)} selected</i>")

    page  = pjob.get("page", 0)
    pages = max(1, (len(pjob["entries"]) + 8 - 1) // 8)
    lines.append(f"<code>Page {page+1}/{pages}</code>")
    return "\n".join(lines)


# ─── Pending job helpers ────────────────────────────────────────────────────

def _get_user_pending_job(bot_data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    """The user's pending format-selection job, or (None, None)."""
    for jid, job in bot_data.get("pending_jobs", {}).items():
        if job.get("user_id") == user_id:
            return jid, job
    return None, None


def _cancel_user_pending(bot_data: dict, user_id: int) -> dict | None:
    """Cancels the user's pending job and returns it (to delete its message)."""
    jid, job = _get_user_pending_job(bot_data, user_id)
    if jid:
        bot_data["pending_jobs"].pop(jid, None)
        return job
    return None


def _get_user_playlist_session(bot_data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    for pid, pjob in bot_data.get("playlist_sessions", {}).items():
        if pjob.get("user_id") == user_id:
            return pid, pjob
    return None, None


# ─── Flow helpers ─────────────────────────────────────────────────────────────

async def _show_youtube_preview(
    *,
    app,
    wait_msg,
    job_id: str,
    info: dict,
) -> None:
    """
    Sends the YouTube info screen: tries the thumbnail photo first (richer
    look), falls back to editing wait_msg as text.
    """
    caption  = build_youtube_info_caption(info, job_id)
    keyboard = build_youtube_action_keyboard(job_id, view="main")
    thumb    = info.get("thumbnail")

    if thumb:
        try:
            sent = await app.bot.send_photo(
                chat_id=wait_msg.chat_id,
                photo=thumb,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                reply_to_message_id=wait_msg.reply_to_message.message_id
                    if wait_msg.reply_to_message else None,
            )
            try:
                await wait_msg.delete()
            except Exception:
                pass
            return sent
        except Exception:
            pass  # thumbnail failed, fall back to text

    await safe_message_edit(
        wait_msg,
        caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return wait_msg


async def _run_playlist_download(
    context,
    pjob_id: str,
    indices: list[int],
    status_msg,
) -> None:
    """Downloads the selected playlist tracks in order; queue_consumer sends
    each item's files as it finishes."""
    bot_data = context.application.bot_data
    pjob     = bot_data.get("playlist_sessions", {}).get(pjob_id)
    if not pjob:
        return

    manager = bot_data["process_manager"]
    mode_dl = "video_best" if pjob.get("type") == "video" else "audio_best"
    total   = len(indices)
    failed: list[str] = []

    # The playlist download is not registered per-item with process_manager
    # (the playlist manages its own ordering); the "downloading" flag on
    # playlist_sessions serves the same purpose.
    pjob["downloading"] = True

    for num, idx in enumerate(indices, start=1):
        if pjob.get("cancelled"):
            break

        entry  = pjob["entries"][idx]
        eurl   = (
            entry.get("webpage_url")
            or entry.get("url")
            or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else "")
        )
        etitle = str(entry.get("title") or entry.get("id") or f"#{idx+1}")[:60]

        if not eurl:
            failed.append(f"{idx+1}. no URL")
            continue

        try:
            await safe_message_edit(
                status_msg,
                f"⬇️ <b>{num}/{total}</b> downloading...\n<i>{_esc(etitle)}</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            job = manager.start_download(
                user_id=pjob["user_id"],
                chat_id=pjob["chat_id"],
                thread_id=pjob["thread_id"],
                reply_to_message_id=pjob["reply_to"],
                url=eurl,
                mode=mode_dl,
            )
            # NOTE: the shared status message is NOT attached to the item's
            # job. queue_consumer deletes status_message_id on completion,
            # which would destroy the playlist's shared progress message.
            # Files are still sent by queue_consumer; only the delete is skipped.

            while True:
                await asyncio.sleep(0.5)
                current = manager.jobs.get(job.job_id)
                if not current or current.done or current.cancelled:
                    break

            if current and current.cancelled:
                failed.append(f"{idx+1}. {etitle}: cancelled")

        except Exception as exc:
            failed.append(f"{idx+1}. {_esc(etitle)}: {str(exc)[:80]}")
            logger.warning("Playlist item error idx=%d: %s", idx, exc)

    pjob["downloading"] = False
    bot_data.get("playlist_sessions", {}).pop(pjob_id, None)

    if failed:
        fail_lines = "\n".join(failed[:10])
        try:
            await safe_message_edit(
                status_msg,
                f"✅ Done ({total - len(failed)}/{total})\n\n"
                f"❌ Failed:\n{_esc(fail_lines)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─── Main handler ─────────────────────────────────────────────────────────────

async def _reject_live(context, *, user, wait_msg, is_admin: bool) -> None:
    """
    Tells the user a livestream link was rejected and records the attempt.

    Attempts 1 and 2 warn, attempt 3 bans temporarily. Admins are exempt.
    """
    guard = context.application.bot_data.get("live_guard")
    text = t("live_not_supported")

    if guard and not is_admin:
        result = await asyncio.get_running_loop().run_in_executor(
            None, guard.register_attempt, user.id
        )
        text = guard_message(result)

    logger.warning("Livestream rejected | user=%s", user.id)

    await safe_message_edit(
        wait_msg,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _try_serve_from_cache(
    context,
    *,
    url: str,
    mode: str,
    chat,
    message,
    silent: bool,
) -> bool:
    """
    Serves a link already downloaded before without a fresh download.
    Returns True when the request was served from cache.
    """
    bot_data = context.application.bot_data
    cache = bot_data.get("media_cache")
    if not cache:
        return False

    record = await asyncio.get_running_loop().run_in_executor(
        None, cache.resolve_sendable, url, mode
    )
    if not record:
        return False

    from bot.sender import send_from_cache  # late import: avoids a circular dependency

    try:
        result_items = await send_from_cache(
            context=context.application,
            chat_id=chat.id,
            thread_id=message.message_thread_id,
            reply_to_message_id=message.message_id,
            record=record,
            source_url=url,
            bare=silent,
        )
    except Exception as exc:
        logger.warning("Cache send failed, falling back to a normal download: %s", exc)
        return False

    # Refresh file_ids (a disk -> re-upload path produces new ones).
    await asyncio.get_running_loop().run_in_executor(
        None, cache.update_file_ids, url, mode, result_items
    )

    registry = bot_data.get("chat_registry")
    if registry:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: registry.record_download(
                chat_id=chat.id,
                title=getattr(chat, "title", None) or "",
                chat_type=chat.type,
                platform=platform_name(url),
            ),
        )
    return True


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message:
        return

    user    = update.effective_user
    message = update.message
    chat    = update.effective_chat

    # ── Permission check (includes ban state) ─────────────────────────────────
    permissions = context.application.bot_data["permissions"]
    check = permissions.check_update(update)
    if not check.allowed:
        if chat.type == "private":
            await safe_reply(message, check.reason)
        return

    # ── Extract URL ─────────────────────────────────────────────────────────
    raw = message.text or message.caption or ""
    url = normalize_url(extract_first_url(raw) or "")
    if not url or not is_supported_url(url):
        return

    # ── Profile/feed link, not a single post: reject before doing any work ──
    # A share button on these platforms often copies the profile URL instead
    # of the post URL; downloading it would mean yt-dlp/gallery-dl silently
    # walking the whole feed instead of a single item.
    # /stories/<user> without a story ID is the same case: it means "the whole
    # tray", not a single story.
    if is_profile_url(url) or instagram_story_kind(url) == "tray":
        if chat.type == "private":
            await safe_reply(
                message,
                t("profile_link_unsupported"),
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
        return

    # ── User/chat record (broadcast list + stats) ──────────────────────────
    # Anyone sending a supported link is recorded, even if the download later
    # fails, so they become a known broadcast target. Writes are buffered
    # instead of hitting disk on every message (see bot/analytics.py).
    activity = context.application.bot_data.get("activity_buffer")
    if activity:
        try:
            await activity.touch_user(
                user.id,
                username=user.username,
                first_name=user.first_name,
                language=user.language_code,
            )
            await activity.touch_chat(
                chat.id,
                title=getattr(chat, "title", None) or "",
                chat_type=chat.type,
            )
        except Exception:
            logger.exception("Activity record failed")

    # ── Temporary ban (livestream spam) ─────────────────────────────────────
    # After the permission check, before a job is started. A ban that expired
    # lifts itself inside ban_remaining().
    live_guard = context.application.bot_data.get("live_guard")
    is_admin = permissions.is_admin(user.id)

    if live_guard and not is_admin:
        remaining = await asyncio.get_running_loop().run_in_executor(
            None, live_guard.ban_remaining, user.id
        )
        if remaining > 0:
            if chat.type == "private":
                await safe_reply(
                    message,
                    t("live_ban_active", duration=format_duration(remaining)),
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            return

    bot_data = context.application.bot_data
    manager  = bot_data["process_manager"]
    config   = bot_data["config"]
    loop     = asyncio.get_running_loop()
    state    = bot_data["bot_state"]

    # ── Maintenance mode: no downloads, a fixed message instead ────────────
    if state.is_maintenance() and not permissions.is_admin(user.id):
        await safe_reply(message, t("maintenance"), reply_to_message_id=message.message_id)
        return

    # ── Safe mode: silent operation, no status/typing/emoji for the user ───
    silent = state.is_safe()

    # ── Cache: serve an already-downloaded link without downloading again ──
    # Direct flows (TikTok/Instagram/Spotify/YT Music, etc.) can be served
    # from cache. The interactive YouTube format menu is never cached.
    if not _is_youtube_url(url) or _is_youtube_music_url(url):
        cache_mode = "audio_best" if (is_spotify_url(url) or _is_youtube_music_url(url)) else "auto"
        if not manager.get_user_active_job(user.id):
            served = await _try_serve_from_cache(
                context, url=url, mode=cache_mode,
                chat=chat, message=message, silent=silent,
            )
            if served:
                return

    # ── Safe mode flow: download silently, reply with the media only ───────
    if silent:
        if manager.get_user_active_job(user.id):
            return  # safe mode sends no "please wait" notice either
        safe_mode_dl = "audio_best" if (is_spotify_url(url) or _is_youtube_music_url(url)) else "auto"
        try:
            manager.start_download(
                user_id=user.id,
                chat_id=chat.id,
                thread_id=message.message_thread_id,
                reply_to_message_id=message.message_id,
                url=url,
                mode=safe_mode_dl,
                silent=True,
                username=user.username,
                chat_title=getattr(chat, "title", None),
                chat_type=chat.type,
            )
            # No status message — safe mode is completely silent.
        except Exception:
            logger.exception("Safe mode download failed to start | url=%s", url)
        return

    # ── Active job check ─────────────────────────────────────────────────────
    if manager.get_user_active_job(user.id):
        await safe_reply(
            message,
            t("wait_active"),
            reply_to_message_id=message.message_id,
        )
        return

    # ── Cancel a pending format menu (a new link arrived) ────────────────────
    # The old menu message is DELETED; it used to only lose its buttons and
    # linger in the chat as clutter.
    await clear_user_pending(context.application, user.id)

    # ── "Analyzing" message ──────────────────────────────────────────────────
    wait_msg = await safe_reply(
        message,
        analyzing_text(url),
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
        disable_web_page_preview=True,
    )
    if not wait_msg:
        return

    plat = platform_name(url)

    try:
        # ── 0. Spotify -> resolve the track on YouTube, download as audio ────
        if is_spotify_url(url):
            if "/track/" not in (url.lower()):
                await safe_message_edit(
                    wait_msg,
                    unsupported_spotify_text(),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            try:
                job = manager.start_download(
                    user_id=user.id,
                    chat_id=chat.id,
                    thread_id=message.message_thread_id,
                    reply_to_message_id=message.message_id,
                    url=url,
                    mode="audio_best",
                    username=user.username,
                    chat_title=getattr(chat, "title", None),
                    chat_type=chat.type,
                )
                manager.attach_status_message(job.job_id, wait_msg.message_id)
                await safe_message_edit(
                    wait_msg,
                    f"{em('icon_spotify')} <i>Preparing Spotify track...</i>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                await safe_message_edit(wait_msg, t("job_start_failed"))
            return

        # ── 1. YouTube playlist ──────────────────────────────────────────────
        if _is_playlist_url(url) and plat in {"YouTube", "YouTube Music"}:
            if chat.type in {"group", "supergroup"}:
                try:
                    me = await context.bot.get_me()
                    bot_url = f"https://t.me/{me.username}"
                except Exception:
                    bot_url = "https://t.me"
                await safe_message_edit(
                    wait_msg,
                    "Playlist downloads are disabled in groups. Open a private chat with the bot for playlists.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Open in private chat", url=bot_url)]
                    ]),
                )
                return

            try:
                raw_info = await loop.run_in_executor(
                    None, _sync_extract_playlist, url, config.cookies_file
                )
            except Exception as exc:
                await safe_message_edit(
                    wait_msg,
                    f"❌ Could not fetch playlist info: {_esc(str(exc)[:200])}",
                    parse_mode="HTML",
                )
                return

            entries = [e for e in (raw_info.get("entries") or []) if isinstance(e, dict)]

            # Livestreams inside the playlist are dropped — a single live
            # entry would stall the whole playlist download indefinitely.
            live_count = sum(1 for e in entries if info_is_live(e))
            if live_count:
                entries = [e for e in entries if not info_is_live(e)]
                logger.info("Dropped %d livestream entries from the playlist", live_count)

            if not entries:
                await safe_message_edit(
                    wait_msg,
                    t("live_not_supported") if live_count
                    else "❌ This playlist is empty or not accessible.",
                    parse_mode="HTML" if live_count else None,
                )
                return

            pjob_id = uuid.uuid4().hex[:12]
            # YouTube Music playlist -> audio mode automatically
            auto_type = "audio" if plat == "YouTube Music" else None

            pjob = {
                "pjob_id": pjob_id,
                "url": url,
                "info": raw_info,
                "entries": entries,
                "platform": plat,
                "user_id": user.id,
                "chat_id": chat.id,
                "thread_id": message.message_thread_id,
                "reply_to": message.message_id,
                "created_at": time.time(),
                "page": 0,
                "selected": set(),
                "type": auto_type,
                "cancelled": False,
                "downloading": False,
            }
            bot_data.setdefault("playlist_sessions", {})[pjob_id] = pjob

            text   = build_playlist_header(pjob)
            markup = (
                build_playlist_track_keyboard(pjob, pjob_id)
                if auto_type
                else _build_playlist_type_keyboard(pjob_id)
            )
            await safe_message_edit(
                wait_msg,
                text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return

        # ── 2 & 3. YouTube / YouTube Music ────────────────────────────────────
        if _is_youtube_url(url):
            # A quick livestream check (extract_flat, ~1.5s) runs before the
            # full metadata fetch. Sequential is faster overall than parallel
            # here: YouTube throttles two concurrent requests, and a live hit
            # skips the expensive metadata fetch entirely.
            # Cookies are deliberately NOT used for this probe: live status is
            # public and a cookie-authenticated query measured 2x slower
            # (3.6s vs 1.5s). A failed probe (age-restricted/private) returns
            # False and the normal flow continues; the worker's cookie-backed
            # check catches it later.
            try:
                probe_live, _probe_info = await loop.run_in_executor(
                    None, probe_is_live, url
                )
            except Exception:
                probe_live = False

            if probe_live:
                await _reject_live(
                    context, user=user, wait_msg=wait_msg, is_admin=is_admin
                )
                return

            try:
                info = await loop.run_in_executor(
                    None, _sync_extract_info, url, config.cookies_file
                )
            except Exception:
                info = {
                    "platform": plat,
                    "title": "",
                    "uploader": "",
                    "description": "",
                    "duration": None,
                    "webpage_url": url,
                    "thumbnail": None,
                }

            # ── Livestream: never start the download ──────────────────────────
            # Metadata was already fetched above, so this check is free. A
            # livestream never ends, which would fill a job slot and the disk
            # forever if allowed through.
            if info.get("is_live"):
                await _reject_live(
                    context, user=user, wait_msg=wait_msg, is_admin=is_admin
                )
                return

            detected_plat = info.get("platform", plat)

            # ── YouTube Music -> automatic audio download ──────────────────────
            if detected_plat == "YouTube Music" or _is_youtube_music_url(url):
                info["platform"] = "YouTube Music"
                yt_music_text = build_youtube_info_caption(info, "")
                try:
                    await safe_message_edit(
                        wait_msg,
                        yt_music_text + "\n\n⏳ <i>Preparing audio...</i>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass

                try:
                    job = manager.start_download(
                        user_id=user.id,
                        chat_id=chat.id,
                        thread_id=message.message_thread_id,
                        reply_to_message_id=message.message_id,
                        url=url,
                        mode="audio_best",
                        username=user.username,
                        chat_title=getattr(chat, "title", None),
                        chat_type=chat.type,
                    )
                    manager.attach_status_message(job.job_id, wait_msg.message_id)
                except Exception:
                    await safe_message_edit(wait_msg, t("job_start_failed"))
                return

            # ── YouTube -> format selection menu ──────────────────────────────
            job_id = uuid.uuid4().hex[:12]

            sent_msg = await _show_youtube_preview(
                app=context.application,
                wait_msg=wait_msg,
                job_id=job_id,
                info=info,
            )

            status_message_id = (
                sent_msg.message_id if sent_msg else wait_msg.message_id
            )
            bot_data.setdefault("pending_jobs", {})[job_id] = {
                "job_id": job_id,
                "user_id": user.id,
                "url": url,
                "info": info,
                "chat_id": chat.id,
                "thread_id": message.message_thread_id,
                "reply_to": message.message_id,
                "created_at": time.time(),
                "status_message_id": status_message_id,
                "username": user.username,
                "chat_title": getattr(chat, "title", None),
                "chat_type": chat.type,
            }
            return

        # ── 4. Every other platform -> direct download ────────────────────────
        try:
            job = manager.start_download(
                user_id=user.id,
                chat_id=chat.id,
                thread_id=message.message_thread_id,
                reply_to_message_id=message.message_id,
                url=url,
                mode="auto",
                username=user.username,
                chat_title=getattr(chat, "title", None),
                chat_type=chat.type,
            )
            manager.attach_status_message(job.job_id, wait_msg.message_id)

            await safe_message_edit(
                wait_msg,
                t("preparing"),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await safe_message_edit(wait_msg, t("job_start_failed"))

    except Exception:
        logger.exception("link_handler unexpected error | user=%s url=%s", user.id, url)
        try:
            await safe_message_edit(
                wait_msg,
                t("err_unexpected"),
            )
        except Exception:
            pass
