from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .emoji_manager import eb, em
from .i18n import t
from .utils import human_bytes, platform_name


# Owner / topluluk linkleri .env'den okunur (configure_branding ile set edilir).
_BRANDING = {
    "show_links": False,
    "owner_link": "",
    "community_link": "",
    "community_label": "Topluluk",
}


def configure_branding(config) -> None:
    """Başlangıçta .env değerlerini UI'a aktarır."""
    _BRANDING["show_links"] = bool(getattr(config, "show_links", False))
    _BRANDING["owner_link"] = getattr(config, "owner_link", "") or ""
    _BRANDING["community_link"] = getattr(config, "community_link", "") or ""
    _BRANDING["community_label"] = getattr(config, "community_label", "Topluluk") or "Topluluk"


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def human_duration(seconds: Any) -> str:
    try:
        if not seconds:
            return t("unknown")
        seconds = int(float(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return t("unknown")


def platform_icon(platform: str) -> str:
    return {
        "YouTube": em("icon_youtube"),
        "YouTube Music": em("icon_ytmusic"),
        "Instagram": em("icon_instagram"),
        "TikTok": em("icon_tiktok"),
        "Facebook": em("icon_facebook"),
        "X/Twitter": em("icon_twitter"),
        "Reddit": em("icon_reddit"),
        "Pinterest": em("icon_pinterest"),
        "Spotify": em("icon_spotify"),
    }.get(platform, em("icon_link"))


def start_text(bot_name: str) -> str:
    return (
        f"<b>{em('brand')} {esc(bot_name)}</b>\n\n"
        f"{t('start_desc')}"
    )


def help_text() -> str:
    return (
        f"<b>{em('menu_help')} {t('help_title')}</b>\n\n"
        f"<b>{t('help_platforms')}</b>\n"
        f"{em('icon_youtube')} YouTube\n"
        f"{em('icon_ytmusic')} YouTube Music\n"
        f"{em('icon_instagram')} Instagram\n"
        f"{em('icon_tiktok')} TikTok\n"
        f"{em('icon_facebook')} Facebook\n"
        f"{em('icon_twitter')} X / Twitter\n"
        f"{em('icon_reddit')} Reddit\n"
        f"{em('icon_pinterest')} Pinterest\n"
        f"{em('icon_spotify')} Spotify\n\n"
        f"<b>{t('help_commands')}</b>\n"
        f"<code>/ses link</code> — {t('help_ses')}\n"
        f"<code>/cancel</code> — {t('help_cancel')}\n\n"
        f"{em('icon_spotify')} {t('help_spotify')}"
    )


def owner_text(enabled: bool, active_jobs: int, banned_users: int, banned_groups: int) -> str:
    return (
        f"<b>{em('menu_owner')} Owner Settings</b>\n\n"
        f"Durum: <b>{'Çalışıyor' if enabled else 'Durduruldu'}</b>\n"
        f"{em('owner_active')} Aktif iş: <b>{active_jobs}</b>\n"
        f"{em('owner_ban')} Banlı kullanıcı: <b>{banned_users}</b>\n"
        f"{em('owner_groups')} Banlı grup: <b>{banned_groups}</b>\n\n"
        "<b>Komutlar</b>\n"
        "<code>/dur</code> — botu durdurur\n"
        "<code>/basla</code> — botu başlatır\n"
        "<code>/status</code> — sistem durumunu gösterir\n"
        "<code>/banid user_id</code> — kullanıcıyı banlar\n"
        "<code>/unbanid user_id</code> — banı kaldırır\n"
        "<code>/emojiler</code> — premium emoji yönetimi"
    )


def start_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(eb("menu_help", "Yardım"), callback_data="menu|help")],
    ]

    # Owner/topluluk linkleri yalnızca .env'de etkinleştirildiyse ve link
    # tanımlıysa gösterilir.
    if _BRANDING["show_links"]:
        link_row = []
        if _BRANDING["owner_link"]:
            link_row.append(InlineKeyboardButton(eb("menu_owner_link", "Owner"), url=_BRANDING["owner_link"]))
        if _BRANDING["community_link"]:
            link_row.append(InlineKeyboardButton(
                eb("menu_mifix", _BRANDING["community_label"]), url=_BRANDING["community_link"]
            ))
        if link_row:
            rows.append(link_row)

    if is_owner:
        rows.append([InlineKeyboardButton(eb("menu_owner", "Admin Paneli"), callback_data="menu|owner")])

    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(eb("menu_back", "Geri"), callback_data="menu|main")]
    ])


def owner_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_key = "btn_stop" if enabled else "btn_start"
    toggle_text = "Durdur" if enabled else "Başlat"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(eb(toggle_key, toggle_text), callback_data="owner|toggle")],
        [InlineKeyboardButton(eb("btn_emoji", "Emoji Yönetimi"), callback_data="emoji|page|0")],
        [InlineKeyboardButton(eb("btn_refresh", "Yenile"), callback_data="menu|owner")],
        [InlineKeyboardButton(eb("menu_back", "Geri"), callback_data="menu|main")],
    ])


def analyzing_text(url: str) -> str:
    return f"{em('status_searching')} {t('analyzing')}"


def worker_started_text(url: str) -> str:
    return f"{em('status_preparing')} {t('preparing')}"


def progress_text(event: dict) -> str:
    status = event.get("status")

    if status == "processing":
        return f"{em('status_processing')} {t('processing')}"

    percent = event.get("percent")

    if percent is None:
        downloaded = human_bytes(event.get("downloaded"))
        total = human_bytes(event.get("total"))
        return (
            f"{em('status_downloading')} {t('downloading')}...\n"
            f"<code>{esc(downloaded)}</code> / <code>{esc(total)}</code>"
        )

    filled = max(0, min(12, int(float(percent) / 100 * 12)))
    bar = "█" * filled + "░" * (12 - filled)

    return (
        f"{em('status_downloading')} <b>{t('downloading')}</b> <code>{float(percent):.1f}%</code>\n"
        f"<code>[{bar}]</code>"
    )


def uploading_text() -> str:
    return f"{em('status_uploading')} {t('uploading')}"


def cancelled_text() -> str:
    return f"{em('status_cancel')} {t('cancelled')}"


def final_caption(title: str, source_url: str) -> str:
    # Sade arayüz: medyanın altında YALNIZCA platform logosu + platform adı.
    # Başlık/açıklama gibi detaylar caption'a konmaz; "Detaylar" butonuyla açılır.
    platform = platform_name(source_url)
    icon = platform_icon(platform)
    return f"{icon} <b>{esc(platform)}</b>"


def build_post_keyboard(post_id: str, source_url: str, has_description: bool = True) -> InlineKeyboardMarkup:
    # Sade arayüz: tek "Detaylar" butonu. Video bilgisi + açıklama
    # varsayılan gizli; kullanıcı basınca açılır. Fazla buton kaldırıldı.
    # has_description geriye dönük uyumluluk için tutulur (detay metninde kullanılır).
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(eb("btn_info", t("btn_details")), callback_data=f"post|{post_id}|details")],
        [InlineKeyboardButton(eb("btn_source", t("btn_source")), url=source_url)],
    ])


def media_info_text(post: dict) -> str:
    info = post.get("info") or {}
    url = post.get("url") or ""
    platform = info.get("platform") or platform_name(url)
    icon = platform_icon(platform)

    unknown = t("unknown")
    title = info.get("title") or post.get("title") or unknown
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or unknown
    uploader_id = info.get("uploader_id") or info.get("channel_id") or info.get("creator_id") or unknown
    duration = human_duration(info.get("duration"))

    width = info.get("width")
    height = info.get("height")
    resolution = info.get("resolution")
    if not resolution and width and height:
        resolution = f"{width}x{height}"
    if not resolution and height:
        resolution = f"{height}p"
    resolution = resolution or unknown

    ext = info.get("ext") or unknown
    quality = info.get("format_note") or info.get("quality") or resolution
    total_size = post.get("total_size")
    size_text = human_bytes(total_size) if total_size else unknown

    lines = [
        f"<b>{icon} {esc(platform)} {t('info_suffix')}</b>",
        "",
        f"{em('field_title')} <b>{t('f_title')}:</b> {esc(title)}",
        f"{em('field_uploader')} <b>{t('f_uploader')}:</b> {esc(uploader)}",
        f"{em('field_uploader_id')} <b>{t('f_uploader_id')}:</b> <code>{esc(uploader_id)}</code>",
        f"{em('field_duration')} <b>{t('f_duration')}:</b> {esc(duration)}",
        f"{em('field_quality')} <b>{t('f_quality')}:</b> {esc(quality)}",
        f"{em('field_format')} <b>{t('f_format')}:</b> {esc(ext)}",
        f"{em('field_size')} <b>{t('f_size')}:</b> {esc(size_text)}",
    ]

    if info.get("view_count"):
        lines.append(f"{em('field_views')} <b>{t('f_views')}:</b> {int(info['view_count']):,}".replace(",", "."))
    if info.get("like_count"):
        lines.append(f"{em('field_likes')} <b>{t('f_likes')}:</b> {int(info['like_count']):,}".replace(",", "."))

    return "\n".join(lines)[:4096]


def _split_long_text(text: str, limit: int = 3500) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []

    parts: list[str] = []
    current = ""

    for line in text.splitlines():
        candidate = f"{current}\n{line}".strip() if current else line
        if len(esc(candidate)) <= limit:
            current = candidate
            continue

        if current:
            parts.append(current.strip())
            current = ""

        while len(esc(line)) > limit:
            cut = max(1000, int(limit * 0.75))
            parts.append(line[:cut].strip())
            line = line[cut:]

        current = line

    if current.strip():
        parts.append(current.strip())

    return parts


def description_messages(post: dict) -> list[str]:
    info = post.get("info") or {}
    desc = str(info.get("description") or "").strip()

    if not desc:
        return []

    return [
        f"{em('field_description')} <b>{t('f_description')}</b>\n\n<blockquote expandable>{esc(part)}</blockquote>"
        for part in _split_long_text(desc)
    ]


def details_messages(post: dict) -> list[str]:
    # Sade arayüz: tek "Detaylar" butonu → video bilgisi + (varsa) açıklama
    # birlikte gönderilir. Varsayılan gizli, basınca açılır.
    messages = [media_info_text(post)]
    messages.extend(description_messages(post))
    return messages


def suspended_facebook_text() -> str:
    return (
        f"{em('icon_facebook')} <b>Facebook geçici olarak askıda.</b>\n\n"
        "Bu platform için Playwright tabanlı yeni fallback daha sonra eklenecek."
    )


def unsupported_spotify_text() -> str:
    return f"{em('icon_spotify')} <b>{t('spotify_unsupported')}</b>"
