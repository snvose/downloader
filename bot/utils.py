from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlparse


URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)

SUPPORTED_DOMAINS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",

    # Facebook algılanır ama indirme akışı askıda.
    "facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com",
    "fb.watch", "fb.com", "www.fb.com",

    "x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "old.reddit.com", "redd.it",
    "pinterest.com", "www.pinterest.com", "pin.it",

    # Spotify bilinçli olarak algılanır ama doğrudan medya indirme yapılmaz.
    "open.spotify.com", "spotify.com", "www.spotify.com",

    # ── Faz 2'de eklenen platformlar ──────────────────────────────────────────
    # Hepsi yt-dlp ile doğrulandı; çoğu cobalt tarafından da destekleniyor
    # (bkz. bot/downloader/cobalt.py SUPPORTED_SERVICES).
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
    "vimeo.com", "www.vimeo.com", "player.vimeo.com",
    "dailymotion.com", "www.dailymotion.com", "dai.ly",
    "twitch.tv", "www.twitch.tv", "m.twitch.tv", "clips.twitch.tv",
    "bsky.app", "www.bsky.app",
    "tumblr.com", "www.tumblr.com",
    "vk.com", "www.vk.com", "m.vk.com", "vkvideo.ru",
    "streamable.com", "www.streamable.com",
    "rutube.ru", "www.rutube.ru",
    "bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv",
    "imgur.com", "www.imgur.com", "i.imgur.com",
    "bandcamp.com",
    "mixcloud.com", "www.mixcloud.com",
    "rumble.com", "www.rumble.com",
    "newgrounds.com", "www.newgrounds.com",
    "loom.com", "www.loom.com",
    "ok.ru", "www.ok.ru",
    "snapchat.com", "www.snapchat.com",
    "kick.com", "www.kick.com",
}

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}


def escape_text(value: object) -> str:
    return html.escape(str(value or ""))


def extract_first_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(1).strip() if match else None


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip(").,]}>\"'")


def get_host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def is_supported_url(url: str) -> bool:
    host = get_host(url)
    if not host:
        return False
    return host in SUPPORTED_DOMAINS or any(host.endswith("." + item) for item in SUPPORTED_DOMAINS)


def platform_name(url: str) -> str:
    host = get_host(url)

    if "music.youtube" in host:
        return "YouTube Music"
    if "youtube" in host or "youtu.be" in host:
        return "YouTube"
    if "instagram" in host:
        return "Instagram"
    if "tiktok" in host:
        return "TikTok"
    if "facebook" in host or host in {"fb.watch", "fb.com", "www.fb.com"}:
        return "Facebook"
    if host in {"x.com", "twitter.com", "www.twitter.com"}:
        return "X/Twitter"
    if "reddit" in host or host == "redd.it":
        return "Reddit"
    if "pinterest" in host or host == "pin.it":
        return "Pinterest"
    if "spotify" in host:
        return "Spotify"

    # ── Faz 2 platformları ────────────────────────────────────────────────────
    # Sıra önemli: daha spesifik eşleşmeler önce gelir.
    if "soundcloud" in host:
        return "SoundCloud"
    if "vimeo" in host:
        return "Vimeo"
    if "dailymotion" in host or host == "dai.ly":
        return "Dailymotion"
    if "twitch" in host:
        return "Twitch"
    if "bsky" in host:
        return "Bluesky"
    if "tumblr" in host:
        return "Tumblr"
    if "vk.com" in host or "vkvideo" in host:
        return "VK"
    if "streamable" in host:
        return "Streamable"
    if "rutube" in host:
        return "Rutube"
    if "bilibili" in host or host == "b23.tv":
        return "Bilibili"
    if "imgur" in host:
        return "Imgur"
    if "bandcamp" in host:
        return "Bandcamp"
    if "mixcloud" in host:
        return "Mixcloud"
    if "rumble" in host:
        return "Rumble"
    if "newgrounds" in host:
        return "Newgrounds"
    if "loom" in host:
        return "Loom"
    if "ok.ru" in host:
        return "OK.ru"
    if "snapchat" in host:
        return "Snapchat"
    if "kick.com" in host:
        return "Kick"

    return "Medya"


def is_spotify_url(url: str) -> bool:
    return "spotify" in get_host(url)


def is_facebook_url(url: str) -> bool:
    host = get_host(url)
    return (
        "facebook" in host
        or host in {"fb.watch", "fb.com", "www.fb.com"}
    )


def human_bytes(num: float | int | None) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "?"


def file_kind(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "document"


def safe_public_error(raw: str) -> str:
    # Teknik hata mesajını kullanıcı diline çevrilmiş, sade bir mesaja eşler.
    from .i18n import t  # geç import (döngüsel bağımlılık önlemi)

    lowered = str(raw or "").lower()

    # "Unsupported URL" önce bakılır. Toplu hata mesajında birden çok denemenin
    # çıktısı yan yana duruyor; aşağıdaki tiktok+403 kuralı çok geniş olduğu
    # için, asıl sebep "bu adres türü desteklenmiyor" olsa bile bir fallback'in
    # 403'ünü yakalayıp kullanıcıya "erişim engeli" diyordu.
    if "unsupported url" in lowered:
        return t("err_unsupported")
    if "tiktok" in lowered and ("403" in lowered or "forbidden" in lowered):
        return t("err_tiktok_403")
    if "only available for registered users" in lowered:
        return t("err_login")
    if "private" in lowered or "login" in lowered or "sign in" in lowered:
        return t("err_private")
    if "requested format is not available" in lowered:
        return t("err_format")
    if "cancel" in lowered or "terminated" in lowered:
        return t("cancelled")

    return t("err_generic")

