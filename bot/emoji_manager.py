from __future__ import annotations

"""
Premium (custom) emoji slots.

Every place the bot renders an emoji is a named slot with a plain-emoji
fallback. The admin can bind a Telegram premium emoji id to any slot; the
bindings live in data/emoji_slots.json.
"""

import json
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
EMOJI_FILE = BASE_DIR / "data" / "emoji_slots.json"


# (slot id, key, fallback emoji, where it shows up)
SLOT_DEFS: list[tuple[int, str, str, str]] = [
    (1, "brand", "⚡", "/start title"),
    (2, "menu_help", "📖", "«Help» button"),
    (3, "menu_owner", "⚙️", "«Admin panel» button"),
    (4, "menu_owner_link", "👤", "«Owner» link button"),
    (5, "menu_mifix", "💬", "«Community» link button"),
    (6, "menu_back", "‹", "«Back» button"),

    (10, "icon_youtube", "▶️", "YouTube"),
    (11, "icon_ytmusic", "🎵", "YouTube Music"),
    (12, "icon_instagram", "📸", "Instagram"),
    (13, "icon_tiktok", "🎶", "TikTok"),
    (14, "icon_facebook", "📘", "Facebook"),
    (15, "icon_reddit", "🤖", "Reddit"),
    (16, "icon_pinterest", "📌", "Pinterest"),
    (17, "icon_twitter", "𝕏", "X / Twitter"),
    (18, "icon_spotify", "🎧", "Spotify"),
    (19, "icon_soundcloud", "🔊", "SoundCloud"),
    (20, "icon_vimeo", "🎬", "Vimeo"),
    (21, "icon_dailymotion", "🎞", "Dailymotion"),
    (22, "icon_twitch", "🎮", "Twitch"),
    (23, "icon_bluesky", "🦋", "Bluesky"),
    (24, "icon_tumblr", "📝", "Tumblr"),
    (25, "icon_snapchat", "👻", "Snapchat"),
    (26, "icon_vk", "🅥", "VK"),
    (27, "icon_rutube", "📺", "Rutube"),
    (28, "icon_bilibili", "📼", "Bilibili"),
    (29, "icon_rumble", "🎯", "Rumble"),
    (30, "icon_streamable", "🎥", "Streamable"),
    (31, "icon_imgur", "🖼", "Imgur"),
    (32, "icon_bandcamp", "🎸", "Bandcamp"),
    (33, "icon_mixcloud", "🎚", "Mixcloud"),
    (34, "icon_newgrounds", "🎨", "Newgrounds"),
    (35, "icon_loom", "💼", "Loom"),
    (36, "icon_okru", "🟠", "OK.ru"),
    (37, "icon_kick", "🥊", "Kick"),
    (38, "icon_link", "🔗", "Unknown platform"),

    (50, "field_title", "🎬", "Details: title line"),
    (51, "field_uploader", "👤", "Details: channel line"),
    (52, "field_uploader_id", "🆔", "Details: channel id line"),
    (53, "field_duration", "⏱", "Details: duration line"),
    (54, "field_quality", "🎛", "Details: quality line"),
    (55, "field_size", "💾", "Details: size line"),
    (56, "field_format", "📦", "Details: format line"),
    (57, "field_views", "👁", "Details: views line"),
    (58, "field_likes", "👍", "Details: likes line"),
    (59, "field_description", "📝", "Details: description line"),

    (70, "btn_info", "ℹ️", "«Details» button under media"),
    (71, "btn_source", "🔗", "«Source» button under media"),
    (72, "btn_emoji", "🎨", "Panel «Emoji» button"),

    (80, "status_searching", "🔎", "«Analyzing link» message"),
    (81, "status_preparing", "⏳", "«Preparing» message"),
    (82, "status_downloading", "📥", "Download progress message"),
    (83, "status_uploading", "📤", "«Uploading» message"),
    (84, "status_processing", "🔄", "«Finishing up» message"),
    (85, "status_cancel", "🛑", "«Cancelled» message"),
    (86, "status_error", "❌", "Error message"),
    (87, "status_done", "✅", "Success message"),

    # Announcement slots are not rendered automatically anywhere; they are the
    # palette an admin writes broadcasts with, via :name: in the draft text.
    (90, "bc_announce", "📣", "Broadcast: :bc_announce:"),
    (91, "bc_new", "🆕", "Broadcast: :bc_new:"),
    (92, "bc_warning", "⚠️", "Broadcast: :bc_warning:"),
    (93, "bc_info", "ℹ️", "Broadcast: :bc_info:"),
    (94, "bc_done", "✅", "Broadcast: :bc_done:"),
    (95, "bc_star", "⭐", "Broadcast: :bc_star:"),
]


ID_TO_SLOT = {sid: (key, fb, ctx) for sid, key, fb, ctx in SLOT_DEFS}
KEY_TO_SLOT = {key: (sid, fb, ctx) for sid, key, fb, ctx in SLOT_DEFS}


def default_slots() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "fallback": fb,
            "context": ctx,
            "custom_id": None,
        }
        for _, key, fb, ctx in SLOT_DEFS
    }


# em() runs on every rendered emoji, so the file is cached in memory and only
# re-read when its mtime changes.
_slots_cache: dict[str, dict[str, Any]] | None = None
_slots_cache_mtime: float = -1.0


def load_slots() -> dict[str, dict[str, Any]]:
    global _slots_cache, _slots_cache_mtime

    try:
        mtime = EMOJI_FILE.stat().st_mtime if EMOJI_FILE.exists() else -1.0
    except OSError:
        mtime = -1.0

    if _slots_cache is not None and mtime == _slots_cache_mtime:
        return _slots_cache

    data = default_slots()

    try:
        if EMOJI_FILE.exists():
            raw = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key not in data:
                        continue

                    if isinstance(value, dict):
                        custom_id = str(value.get("custom_id") or "").strip()
                    else:
                        custom_id = str(value or "").strip()

                    data[key]["custom_id"] = custom_id if custom_id.isdigit() else None
    except Exception:
        pass

    _slots_cache = data
    _slots_cache_mtime = mtime
    return data


def save_slots(data: dict[str, Any]) -> None:
    global _slots_cache, _slots_cache_mtime
    _slots_cache = None
    _slots_cache_mtime = -1.0
    full = default_slots()

    for key, value in (data or {}).items():
        if key not in full:
            continue

        if isinstance(value, dict):
            custom_id = str(value.get("custom_id") or "").strip()
        else:
            custom_id = str(value or "").strip()

        full[key]["custom_id"] = custom_id if custom_id.isdigit() else None

    EMOJI_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMOJI_FILE.write_text(
        json.dumps(full, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_file() -> None:
    if not EMOJI_FILE.exists():
        save_slots(default_slots())


def fallback(key: str, default: str = "") -> str:
    slot = KEY_TO_SLOT.get(key)
    return slot[1] if slot else default


def em(key: str, default: str | None = None) -> str:
    fb = fallback(key, default or "")

    try:
        custom_id = load_slots().get(key, {}).get("custom_id")
        if custom_id and str(custom_id).isdigit():
            # Telegram expects a real emoji character inside tg-emoji; a symbol
            # fallback such as "‹" or "𝕏" triggers Entity_text_invalid.
            return f'<tg-emoji emoji-id="{custom_id}">✨</tg-emoji>'
    except Exception:
        pass

    return fb


# Slot names written into a broadcast draft, e.g. ":bc_new: version 2 is out".
_SLOT_TOKEN_RE = re.compile(r":([a-z][a-z0-9_]{1,31}):")

# <tg-emoji emoji-id="123">✨</tg-emoji> — matched to undo premium emoji when
# Telegram refuses them.
_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.DOTALL)


def _inner(fb: str) -> str:
    """What goes inside a tg-emoji tag as the non-premium stand-in.

    Telegram rejects the entity unless the tag wraps a real emoji, so slots
    whose fallback is a plain character ("‹", "𝕏") borrow a generic one.
    """
    return fb if fb and max(fb) > "\u2000" else "✨"


def em_html(key: str) -> str:
    """Like em(), but keeps the slot's own fallback inside the tag.

    That makes the premium emoji degrade to the right plain emoji instead of a
    generic sparkle when the entity has to be stripped later.
    """
    fb = fallback(key, "")
    try:
        custom_id = load_slots().get(key, {}).get("custom_id")
        if custom_id and str(custom_id).isdigit():
            return f'<tg-emoji emoji-id="{custom_id}">{_inner(fb)}</tg-emoji>'
    except Exception:
        pass
    return fb


def render_slots(text: str) -> tuple[str, list[str]]:
    """Replaces :slot_name: tokens with that slot's emoji.

    Returns the rendered text and the slot names that were used. Unknown
    tokens are left alone — ":30:" in a sentence is not a slot.
    """
    used: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in KEY_TO_SLOT:
            return match.group(0)
        if key not in used:
            used.append(key)
        return em_html(key)

    return _SLOT_TOKEN_RE.sub(replace, text or ""), used


def strip_premium_emoji(text: str) -> str:
    """Turns every premium emoji back into the plain emoji inside its tag."""
    return _TG_EMOJI_RE.sub(lambda m: m.group(1), text or "")


def broadcast_slots() -> list[tuple[int, str, str, str]]:
    """The announcement palette, for the compose screen's cheat sheet."""
    return [slot for slot in SLOT_DEFS if 90 <= slot[0] <= 99]


def eb(key: str, text: str = "") -> str:
    # Button labels do not support parse_mode, so buttons always use the
    # fallback emoji rather than a premium entity.
    fb = fallback(key, "")
    return f"{fb} {text}".strip()


def set_slot(key: str, custom_id: str) -> None:
    if key not in KEY_TO_SLOT:
        raise KeyError(key)

    custom_id = str(custom_id or "").strip()
    if not custom_id.isdigit():
        raise ValueError("custom_id must be numeric")

    data = load_slots()
    data[key]["custom_id"] = custom_id
    save_slots(data)


def reset_slot(key: str) -> None:
    if key not in KEY_TO_SLOT:
        raise KeyError(key)

    data = load_slots()
    data[key]["custom_id"] = None
    save_slots(data)


def reset_all_slots() -> int:
    """Clears every premium emoji binding. Returns how many were cleared."""
    data = load_slots()
    count = sum(1 for v in data.values() if v.get("custom_id"))
    for key in data:
        data[key]["custom_id"] = None
    save_slots(data)
    return count


def assigned_count() -> int:
    return sum(1 for value in load_slots().values() if value.get("custom_id"))


# Slot id ranges used to group the admin panel listing.
SLOT_CATEGORIES: list[tuple[int, int, str]] = [
    (1, 9, "Menu"),
    (10, 49, "Platform icons"),
    (50, 69, "Info fields"),
    (70, 79, "Buttons"),
    (80, 89, "Status messages"),
    (90, 99, "Announcements"),
]


def category_for(sid: int) -> str:
    for lo, hi, name in SLOT_CATEGORIES:
        if lo <= sid <= hi:
            return name
    return "Other"
