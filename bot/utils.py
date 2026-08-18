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
    "facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com",
    "fb.watch", "fb.com", "www.fb.com",
    "x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "old.reddit.com", "redd.it",
    "pinterest.com", "www.pinterest.com", "pin.it",

    # Spotify is detected on purpose but has no direct media download; the
    # worker looks the track up on YouTube instead.
    "open.spotify.com", "spotify.com", "www.spotify.com",

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

    # Order matters: more specific matches come first.
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

    return "Media"


def is_spotify_url(url: str) -> bool:
    return "spotify" in get_host(url)


# Platforms where a bare profile/feed URL (no post/video ID in the path) is
# easy to send by mistake — a share button copies the profile link, not the
# post link, more often than users notice. Matching one of these means the
# path actually points at a single post; anything else on these platforms is
# treated as a profile link and rejected before a download ever starts.
_POST_PATH_PATTERNS: dict[str, re.Pattern] = {
    "Instagram": re.compile(r"/(p|reel|reels|tv|stories)/", re.IGNORECASE),
    "TikTok": re.compile(r"/(video|photo)/\d+", re.IGNORECASE),
    "X/Twitter": re.compile(r"/status(es)?/\d+", re.IGNORECASE),
    "Facebook": re.compile(
        r"/(videos|reel|watch|posts|photo|share)/|story_fbid=|photo\.php|permalink\.php|[?&]v=\d+",
        re.IGNORECASE,
    ),
    "Reddit": re.compile(r"/comments/", re.IGNORECASE),
    "Pinterest": re.compile(r"/pin/", re.IGNORECASE),
}

# Short-link redirectors on these platforms always resolve to a single post,
# never a profile page, so they're exempt from the path check above.
_ALWAYS_POST_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "fb.watch", "pin.it", "redd.it"}


def is_profile_url(url: str) -> bool:
    """True if url looks like a whole profile/feed page rather than a single post."""
    pattern = _POST_PATH_PATTERNS.get(platform_name(url))
    if pattern is None:
        return False
    if get_host(url) in _ALWAYS_POST_HOSTS:
        return False
    parsed = urlparse(url)
    if pattern.search(parsed.path or "") or pattern.search(parsed.query or ""):
        return False
    return True


def is_facebook_url(url: str) -> bool:
    host = get_host(url)
    return "facebook" in host or host in {"fb.watch", "fb.com", "www.fb.com"}


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
    """Maps a technical error message to a short, translated user message."""
    from .i18n import t  # late import: avoids a circular dependency

    lowered = str(raw or "").lower()

    # "Unsupported URL" is checked first: a combined error message contains the
    # output of several attempts, and the broader rules below would otherwise
    # report a fallback's 403 as the real cause.
    if "unsupported url" in lowered:
        return t("err_unsupported")
    # Account lock is checked before "login required": both can appear in the
    # same message but this is the one the user needs to hear.
    if "checkpoint_required" in lowered or "challenge_required" in lowered:
        return t("err_ig_checkpoint")
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
