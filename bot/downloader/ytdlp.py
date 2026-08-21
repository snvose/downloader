from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from http.cookiejar import MozillaCookieJar
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import yt_dlp

from bot.downloader.metadata import apply_audio_metadata
from bot.live_guard import info_is_live, probe_is_live
from bot.queue_events import cookie_event, log_event, progress_event
from bot.utils import instagram_story_kind, platform_name


class LiveStreamError(RuntimeError):
    """A livestream was detected — the download is never started."""


# Hard cap on how much disk a single download may use (bytes).
# Last line of defense against endless streams: yt-dlp stops once this is hit.
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_SUFFIXES = (
    ".part",
    ".ytdl",
    ".temp",
    ".tmp",
    ".info.json",
    ".description",
    ".json",
    ".lrc",
)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS

# Social platforms can contain multiple media items (carousel/slideshow), so
# noplaylist is disabled for them and gallery-dl is tried if yt-dlp fails.
SOCIAL_PLATFORMS = {
    "Instagram",
    "TikTok",
    "Reddit",
    "Pinterest",
    "X/Twitter",
    "Facebook",
    "Tumblr",
    "Imgur",
    "Bluesky",
    "Snapchat",
}


def collect_files(download_dir: Path, *, mode: str = "auto") -> list[str]:
    files: list[str] = []

    # Thumbnail mode only collects image files.
    target_exts = IMAGE_EXTS if mode == "thumbnail" else MEDIA_EXTS

    for root, _, names in os.walk(download_dir):
        for name in names:
            lowered = name.lower()
            if any(lowered.endswith(item) for item in SKIP_SUFFIXES):
                continue

            path = Path(root) / name
            ext = path.suffix.lower()

            if ext not in target_exts:
                continue

            try:
                if path.is_file() and path.stat().st_size > 0:
                    files.append(str(path))
            except OSError:
                pass

    files.sort()

    if mode.startswith("audio"):
        audio_files = [item for item in files if Path(item).suffix.lower() in AUDIO_EXTS]
        if audio_files:
            return audio_files

    return files


def _gallery_command() -> list[str]:
    binary = shutil.which("gallery-dl")
    if binary:
        return [binary]
    return [sys.executable, "-m", "gallery_dl"]


def _download_with_gallery_dl(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    cookies_file: Path | None,
    queue: Any,
) -> tuple[list[str], str, dict[str, Any]]:
    queue.put(log_event(job_id, "warning", "Trying the gallery-dl fallback."))

    cmd = _gallery_command()
    cmd += ["-d", str(download_dir)]
    # Left at its defaults gallery-dl answers a 429 by sleeping a minute and
    # retrying four times, so a rate-limited fallback held the job — and its
    # download slot — for over four minutes before failing anyway.
    cmd += ["--retries", "2", "--sleep-429", "5"]

    if cookies_file and cookies_file.exists():
        cmd += ["--cookies", str(cookies_file)]

    cmd.append(url)

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if stdout:
        queue.put(log_event(job_id, "info", "gallery-dl stdout: " + stdout[-700:]))

    if proc.returncode != 0:
        raise RuntimeError("gallery-dl failed: " + (stderr or stdout or "unknown error")[-1000:])

    files = collect_files(download_dir)
    if not files:
        raise RuntimeError("gallery-dl downloaded no files.")

    # Videos from gallery-dl go through the same compatibility layer; this
    # branch used to be skipped and its files still came out as vp9/av1.
    files = _ensure_playable(files, job_id=job_id, queue=queue)

    info = {
        "platform": platform_name(url),
        "title": platform_name(url),
        "webpage_url": url,
        "description": "",
    }

    return files, platform_name(url), info


def _cookie_names_for_domain(cookie_file: Path, domain_keyword: str) -> list[str]:
    names: list[str] = []

    if not cookie_file.exists():
        return names

    try:
        for line in cookie_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                parts = line.split()

            if len(parts) < 7:
                continue

            domain = parts[0].lower()
            name = parts[5]

            if domain_keyword.lower() in domain and name not in names:
                names.append(name)
    except Exception:
        return names

    return sorted(names)


# The magic header line REQUIRED at the top of a Netscape cookie file. Both
# yt-dlp and Python's MozillaCookieJar refuse to read the file without it and
# say "does not look like a Netscape format cookies file".
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


def _repair_cookie_header(cookies_file: Path, job_id: str, queue: Any) -> None:
    """
    Restores the magic header at the top of the cookie file if it's missing.

    Pasting new cookies at the very top of the file overwrites the header
    line. The result used to break EVERY platform at once: even with
    perfectly valid content, yt-dlp rejected the file and no cookie-based
    download worked, YouTube included.

    The repair only happens when the body actually looks like valid cookie
    lines — patching a broken file and calling it "fixed" would be worse.
    """
    try:
        text = cookies_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return

    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# Netscape") or stripped.startswith("# HTTP Cookie File"):
            return  # header is already there
        break

    # Is the body valid? At least one line must have 7 tab-separated fields.
    data_lines = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        if len(line.split("\t")) == 7:
            data_lines += 1
        else:
            queue.put(log_event(
                job_id, "warning",
                "Cookie file format looks broken (a line isn't tab-separated) — "
                "re-export it from your browser extension.",
            ))
            return

    if not data_lines:
        return

    try:
        cookies_file.write_text(
            _NETSCAPE_HEADER + "\n# Auto-repaired: missing header line restored.\n" + text,
            encoding="utf-8",
        )
    except OSError as exc:
        queue.put(log_event(job_id, "warning", f"Could not repair the cookie header: {short_error(exc)}"))
        return

    queue.put(log_event(
        job_id, "warning",
        f"The '{_NETSCAPE_HEADER}' header was missing from the cookie file, "
        f"restored automatically ({data_lines} cookies kept).",
    ))


def _log_cookie_status(job_id: str, url: str, cookies_file: Path | None, queue: Any) -> None:
    if not cookies_file:
        queue.put(log_event(job_id, "warning", "No cookie file configured."))
        return

    if not cookies_file.exists():
        queue.put(log_event(job_id, "warning", f"Cookie file not found: {cookies_file}"))
        return

    try:
        size = cookies_file.stat().st_size
    except OSError:
        size = 0

    queue.put(log_event(job_id, "info", f"Cookie file active: {cookies_file} ({size} bytes)"))

    _repair_cookie_header(cookies_file, job_id, queue)

    host = (urlparse(url).netloc or "").lower()

    if "facebook" in host or "fb.watch" in host:
        names = _cookie_names_for_domain(cookies_file, "facebook")
        safe_names = ", ".join(names[:20]) if names else "none"
        queue.put(log_event(job_id, "info", f"Facebook cookie names: {safe_names}"))

    if "tiktok" in host:
        names = _cookie_names_for_domain(cookies_file, "tiktok")
        safe_names = ", ".join(names[:20]) if names else "none"
        queue.put(log_event(job_id, "info", f"TikTok cookie names: {safe_names}"))


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _first_format_value(info: dict, key: str) -> Any:
    requested = info.get("requested_downloads") or info.get("requested_formats") or []

    if isinstance(requested, dict):
        requested = [requested]

    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict) and item.get(key):
                return item.get(key)

    formats = info.get("formats") or []
    if isinstance(formats, list):
        for item in reversed(formats):
            if isinstance(item, dict) and item.get(key):
                return item.get(key)

    return None


def _compact_info(info: dict, url: str) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {"platform": platform_name(url), "webpage_url": url}

    width = info.get("width") or _first_format_value(info, "width")
    height = info.get("height") or _first_format_value(info, "height")
    ext = info.get("ext") or _first_format_value(info, "ext")
    format_note = info.get("format_note") or _first_format_value(info, "format_note")

    resolution = None
    if width and height:
        resolution = f"{width}x{height}"
    elif height:
        resolution = f"{height}p"

    description = info.get("description") or info.get("alt_title") or ""
    if isinstance(description, list):
        description = "\n".join(str(x) for x in description if x)

    webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)

    return {
        "platform": platform_name(webpage_url),
        "title": str(info.get("title") or info.get("fulltitle") or ""),
        "description": str(description or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or info.get("creator") or ""),
        "uploader_id": str(info.get("uploader_id") or info.get("channel_id") or info.get("creator_id") or ""),
        "channel": str(info.get("channel") or ""),
        "duration": _safe_int(info.get("duration")),
        "webpage_url": webpage_url,
        "extractor": str(info.get("extractor") or info.get("extractor_key") or ""),
        "ext": str(ext or ""),
        "width": _safe_int(width),
        "height": _safe_int(height),
        "resolution": resolution,
        "format_note": str(format_note or ""),
        "vcodec": str(info.get("vcodec") or _first_format_value(info, "vcodec") or ""),
        "acodec": str(info.get("acodec") or _first_format_value(info, "acodec") or ""),
        "filesize": _safe_int(info.get("filesize") or _first_format_value(info, "filesize")),
        "filesize_approx": _safe_int(info.get("filesize_approx") or _first_format_value(info, "filesize_approx")),
        "thumbnail": str(info.get("thumbnail") or ""),
        "view_count": _safe_int(info.get("view_count")),
        "like_count": _safe_int(info.get("like_count")),
        "artist": str(info.get("artist") or ""),
        "album": str(info.get("album") or ""),
        "release_year": _safe_int(info.get("release_year")),
    }


def _is_social_url(url: str) -> bool:
    return platform_name(url) in SOCIAL_PLATFORMS


def _is_single_story(url: str) -> bool:
    """A link to one Instagram story. Without noplaylist yt-dlp would fetch the
    poster's entire story tray for it."""
    return instagram_story_kind(url) == "single"


def _is_spotify_url(url: str) -> bool:
    return "spotify" in (urlparse(url).netloc or "").lower()


def _is_instagram_url(url: str) -> bool:
    return "instagram" in (urlparse(url).netloc or "").lower()


# TTL cache for the Instagram session state: (timestamp, state).
# Avoids a network request per job and repeatedly poking a locked account.
_IG_SESSION_CACHE: tuple[float, str] = (0.0, "")
_IG_SESSION_TTL = 15 * 60
# The panel notification for a lock is rate-limited to this interval.
_IG_CHECKPOINT_REPORTED = 0.0

# Last time the panel was told about a logged-out session (rate-limits the
# report the same way the checkpoint one is limited).
_IG_LOGGED_OUT_REPORTED = 0.0

IG_LOGGED_OUT_MESSAGE = (
    "The Instagram cookie has no valid session (the sessionid cookie is "
    "missing or expired), so every request goes out logged out. Public reels "
    "still download, but age-gated or restricted-audience posts and stories "
    "do not, and Instagram rate-limits anonymous traffic hard. Re-export "
    "cookies.txt from a browser that is logged in to Instagram."
)

IG_CHECKPOINT_MESSAGE = (
    "The Instagram account is locked (checkpoint_required) — the cookie's "
    "owner account is waiting for verification at instagram.com/challenge/. "
    "Cookie-based Instagram downloads won't work until a human completes "
    "the verification and the cookie is re-exported."
)


def _instagram_session_state(cookies_file: Path | None) -> str:
    """
    Returns the state of the Instagram session in cookies.txt.

    "ok"          — the session works
    "checkpoint"  — the account is locked, Instagram wants verification
    "logged_out"  — the session isn't recognized (redirected to login)
    "unknown"     — could not be determined (network error, no cookie, etc.)

    WHY THIS IS NEEDED: on a locked account, all yt-dlp sees is
    "HTTP Error 400: Bad Request"; the real reason is in the response BODY,
    which yt-dlp doesn't surface. Without reading the body this was
    indistinguishable from an ordinary network error — no warning in the
    admin panel, and the user just saw "HTTP Error 400".
    """
    global _IG_SESSION_CACHE

    if not cookies_file or not Path(cookies_file).exists():
        return "unknown"

    cached_at, cached = _IG_SESSION_CACHE
    if cached and time.time() - cached_at < _IG_SESSION_TTL:
        return cached

    state = "unknown"
    try:
        jar = MozillaCookieJar(str(cookies_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        opener = build_opener(HTTPCookieProcessor(jar))
        # feed/timeline/ requires a session, doesn't need a media id, and
        # returns the checkpoint body directly on a locked account.
        request = Request(
            "https://i.instagram.com/api/v1/feed/timeline/",
            headers={
                "User-Agent": HTTP_HEADERS.get("User-Agent", "Mozilla/5.0"),
                "X-IG-App-ID": "936619743392459",
                "Accept": "*/*",
            },
        )
        try:
            response = opener.open(request, timeout=20)
            body = response.read(4096).decode("utf-8", "replace")
            final_url = response.url
        except HTTPError as exc:
            body = exc.read(4096).decode("utf-8", "replace") if hasattr(exc, "read") else ""
            final_url = getattr(exc, "url", "") or ""

        lowered = (body or "").lower()
        if "checkpoint_required" in lowered or "challenge_required" in lowered:
            state = "checkpoint"
        elif "/accounts/login" in (final_url or ""):
            state = "logged_out"
        elif body:
            state = "ok"
    except Exception:
        state = "unknown"

    _IG_SESSION_CACHE = (time.time(), state)
    return state


def _spotify_embed_data(url: str) -> dict[str, Any]:
    """
    Reads the __NEXT_DATA__ JSON blob from the Spotify embed page.

    This page needs no API key and gives the track's REAL details: name,
    artist(s), release date and a 640x640 cover — unlike the channel name /
    upload date / video thumbnail we'd get from YouTube.
    """
    m = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not m:
        return {}

    embed = f"https://open.spotify.com/embed/track/{m.group(1)}"
    req = Request(embed, headers=HTTP_HEADERS)
    with urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", errors="ignore")

    blob = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page, re.DOTALL,
    )
    if not blob:
        return {"_page": page}

    import json as _json
    data = _json.loads(blob.group(1))
    entity = (
        data.get("props", {}).get("pageProps", {})
        .get("state", {}).get("data", {}).get("entity", {})
    )
    if not isinstance(entity, dict):
        return {"_page": page}

    entity["_page"] = page
    return entity


def _spotify_cover_url(entity: dict[str, Any]) -> str:
    """Picks the largest album cover from the embed data."""
    images = (entity.get("visualIdentity") or {}).get("image") or []
    best = ""
    best_size = 0
    for item in images:
        if not isinstance(item, dict):
            continue
        size = int(item.get("maxWidth") or 0)
        if size > best_size and item.get("url"):
            best, best_size = str(item["url"]), size
    return best


def _spotify_track_info(url: str, queue: Any, job_id: str) -> dict[str, Any]:
    """
    Reads a Spotify track page for the track's credits.

    yt-dlp can't download audio from Spotify (DRM), so the spotdl approach is
    used: read Spotify metadata -> search YouTube -> download audio from
    there.

    Returns {"query", "title", "artist", "release_date", "cover_url"}.
    Previously only the search text ("query") was returned and the rest of
    the credits was discarded, so the file ended up tagged with YouTube's
    data instead (channel name "Hidra Official" instead of the artist, the
    video's upload date instead of the release year, YouTube's category
    "Entertainment" instead of a genre). The real credits are kept now.
    """
    path = (urlparse(url).path or "").lower()
    if "/track/" not in path:
        raise RuntimeError(
            "Only single track Spotify links can be downloaded. "
            "Albums/playlists are not supported."
        )

    title = ""
    author = ""
    release_date = ""
    cover_url = ""
    page = ""

    # ── 1. Preferred source: the embed page's structured JSON ──
    try:
        entity = _spotify_embed_data(url)
        page = str(entity.pop("_page", "") or "")
        title = str(entity.get("name") or entity.get("title") or "").strip()

        artists = entity.get("artists")
        if isinstance(artists, (list, tuple)):
            names = [
                str(a.get("name")).strip() for a in artists
                if isinstance(a, dict) and a.get("name")
            ]
            author = ", ".join(names)

        iso = (entity.get("releaseDate") or {}).get("isoString")
        if iso:
            release_date = str(iso)[:10]

        cover_url = _spotify_cover_url(entity)
    except Exception as exc:
        queue.put(log_event(job_id, "warning", f"Could not read the Spotify embed: {exc}"))

    # ── 2. Fallback: the oEmbed endpoint (gives a title, not an artist) ──
    if not title:
        oembed = "https://open.spotify.com/oembed?url=" + url
        try:
            req = Request(oembed, headers=HTTP_HEADERS)
            with urlopen(req, timeout=20) as resp:
                import json as _json
                data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
            title = str(data.get("title") or "").strip()
            if not author:
                author = str(data.get("author_name") or "").strip()
        except Exception as exc:
            queue.put(log_event(job_id, "warning", f"Spotify oembed failed: {exc}"))

    # ── 3. Last resort: regex over the embed page's raw HTML ──
    # Kept separate so a change in __NEXT_DATA__'s shape doesn't break the
    # whole flow.
    if page and (not title or not author):
        if not author:
            m = (re.search(r'"artists":\[\{"name":"([^"]+)"', page)
                 or re.search(r'"name":"([^"]+)","uri":"spotify:artist', page))
            if m:
                author = html.unescape(m.group(1)).strip()
        if not title:
            m = (re.search(r'"name":"([^"]+)","uri":"spotify:track', page)
                 or re.search(r'"title":"([^"]+)"', page))
            if m:
                title = html.unescape(m.group(1)).strip()

    if not title:
        raise RuntimeError("Could not fetch Spotify track info.")

    query = f"{title} {author}".strip()
    queue.put(log_event(job_id, "info", f"Spotify -> YouTube search: {query}"))

    return {
        "query": query,
        "title": title,
        "artist": author,
        "release_date": release_date,
        "cover_url": cover_url,
    }


def _strip_youtube_tags(path: Path) -> None:
    """
    Removes the tags ffmpeg wrote from YouTube data that do NOT belong to the
    track.

    The FFmpegMetadata postprocessor writes whatever it finds on the video
    page: YouTube's category ("Entertainment") into TCON, a video link into
    the comment, the channel's social links into the description, the
    video's upload date into TDRC. All of that is wrong on a music file. The
    correct data comes from Spotify; a field that isn't provided there is
    left empty rather than guessed.
    """
    if path.suffix.lower() != ".mp3":
        return

    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        audio = ID3(str(path))
    except (ID3NoHeaderError, Exception):
        return

    for frame in ("TCON", "COMM", "TXXX", "TDRC", "TDRL", "TYER"):
        audio.delall(frame)

    try:
        audio.save(str(path))
    except Exception:
        pass


def _apply_spotify_metadata(
    files: list[str],
    track: dict[str, Any],
    *,
    queue: Any,
    job_id: str,
) -> None:
    """
    REPLACES the downloaded audio's tags with the Spotify credits.

    The audio comes from YouTube, so yt-dlp/ffmpeg wrote YouTube's data onto
    it: the channel name instead of the artist, the upload date instead of
    the year, YouTube's category instead of a genre, the video title instead
    of the track title. Since the user sent a Spotify link, the correct
    credits are Spotify's.

    The cover is also replaced with Spotify's 640x640 square cover; the
    YouTube thumbnail is a 16:9 video frame that doesn't look like an album
    cover once cropped.
    """
    audio = [Path(f) for f in files if Path(f).suffix.lower() in AUDIO_EXTS]
    if not audio:
        return

    info: dict[str, Any] = {}
    if track.get("title"):
        info["track"] = track["title"]
    if track.get("artist"):
        info["artist"] = track["artist"]
        info["album_artist"] = track["artist"].split(",")[0].strip()
    if track.get("release_date"):
        info["release_date"] = track["release_date"]

    for path in audio:
        _strip_youtube_tags(path)

        # Write the Spotify cover next to the audio file as .jpg; the
        # metadata layer looks for it there and finds .jpg before .webp.
        cover_url = track.get("cover_url")
        if cover_url:
            try:
                req = Request(cover_url, headers=HTTP_HEADERS)
                with urlopen(req, timeout=20) as resp:
                    data = resp.read()
                if data:
                    path.with_suffix(".jpg").write_bytes(data)
            except Exception as exc:
                queue.put(log_event(
                    job_id, "warning", f"Could not download the Spotify cover: {exc}"
                ))

        written = apply_audio_metadata(path, info, job_id=job_id)
        if written:
            queue.put(log_event(
                job_id, "info",
                "Spotify credits written: " + ", ".join(sorted(written)),
            ))


def _is_tiktok_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "tiktok" in host


# ── TikTok photo (slideshow) posts ────────────────────────────────────────
# TikTok has two kinds of posts: /video/ and /photo/. yt-dlp only recognizes
# the first — TikTokIE._VALID_URL never matches /photo/, and the extractor
# has no code reading the imagePost field at all. The result: a photo post
# fails with "Unsupported URL", and gallery-dl also rewrites the URL to
# /video/ and gets a 403 (it can't solve TikTok's JS challenge). So slideshow
# posts were entirely undownloadable.
#
# Fix: rewrite the URL to /video/ and let yt-dlp fetch the page — its
# challenge-solving code already works there. The images live in the raw
# data that step produces (imagePost.images); we read and download them.

_TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}


def _resolve_tiktok_url(url: str) -> str:
    """
    Resolves a vt./vm. short link to its real address.

    A short link doesn't say whether it's a photo or a video post; the only
    way to know which branch to take is to follow the redirect.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    is_short = host in _TIKTOK_SHORT_HOSTS or (
        host.endswith("tiktok.com") and parsed.path.startswith("/t/")
    )
    if not is_short:
        return url

    # HEAD is enough: only the final redirected address is needed, not the
    # page body (no point downloading ~400 KB per short link).
    try:
        with urlopen(Request(url, headers=HTTP_HEADERS, method="HEAD"), timeout=15) as resp:
            return resp.url or url
    except Exception:
        return url


def _tiktok_photo_id(url: str) -> str:
    """Returns the post id if this is a photo post, else an empty string."""
    if not _is_tiktok_url(url):
        return ""
    match = re.search(r"/photo/(\d+)", urlparse(url).path)
    return match.group(1) if match else ""


def _tiktok_photo_detail(
    video_url: str,
    photo_id: str,
    *,
    cookies_file: Path | None,
    use_cookies: bool,
    queue: Any,
    job_id: str,
) -> dict[str, Any]:
    """
    Returns the raw TikTok data for a photo post.

    Calls into yt-dlp's internal method; AttributeError is handled
    separately in case a version upgrade renames it, so the download doesn't
    just crash.
    """
    from yt_dlp.extractor.tiktok import TikTokIE

    opts = _build_opts(
        job_id=job_id,
        url=video_url,
        download_dir=Path("."),
        queue=queue,
        cookies_file=cookies_file,
        mode="video_best",
        use_cookies=use_cookies,
        format_profile="normal",
    )
    opts.pop("progress_hooks", None)
    opts["skip_download"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        extractor = TikTokIE(ydl)
        extractor.initialize()
        try:
            detail, _status = extractor._extract_web_data_and_status(video_url, photo_id)
        except AttributeError as exc:
            raise RuntimeError(
                f"yt-dlp's internal TikTok interface changed, couldn't read the "
                f"photo post: {exc}"
            ) from exc

    return detail if isinstance(detail, dict) else {}


def _tiktok_photo_urls(detail: dict[str, Any]) -> list[str]:
    """Extracts image URLs, in order, from imagePost.images[]."""
    images = ((detail.get("imagePost") or {}).get("images")) or []
    urls: list[str] = []
    for image in images:
        candidates = ((image.get("imageURL") or {}).get("urlList")) or []
        for candidate in candidates:
            if candidate:
                urls.append(str(candidate))
                break
    return urls


def _download_tiktok_photos(
    *,
    job_id: str,
    url: str,
    video_url: str,
    photo_id: str,
    download_dir: Path,
    queue: Any,
    cookies_file: Path | None,
) -> tuple[list[str], str, dict[str, Any]]:
    """Downloads the images of a slideshow post."""
    errors: list[str] = []
    detail: dict[str, Any] = {}

    for label, use_cookies in (("cookies", True), ("cookieless", False)):
        try:
            queue.put(log_event(job_id, "info", f"TikTok photo attempt: {label}"))
            detail = _tiktok_photo_detail(
                video_url, photo_id,
                cookies_file=cookies_file, use_cookies=use_cookies,
                queue=queue, job_id=job_id,
            )
            if _tiktok_photo_urls(detail):
                break
            errors.append(f"{label}: no images found in the post")
        except Exception as exc:
            message = short_error(exc)
            errors.append(f"{label}: {message}")
            queue.put(log_event(
                job_id, "warning", f"TikTok photo attempt failed [{label}]: {message}",
            ))

    image_urls = _tiktok_photo_urls(detail)
    if not image_urls:
        raise RuntimeError(
            "Could not read the TikTok slideshow's images — " + " | ".join(errors)
        )

    title = str((detail.get("desc") or "")).strip()
    uploader = str(((detail.get("author") or {}).get("uniqueId") or "")).strip()

    files: list[str] = []
    for index, image_url in enumerate(image_urls, start=1):
        target = Path(download_dir) / f"tiktok_{photo_id}_{index:02d}.jpg"
        try:
            with urlopen(Request(image_url, headers=HTTP_HEADERS), timeout=60) as resp:
                data = resp.read()
        except Exception as exc:
            queue.put(log_event(
                job_id, "warning",
                f"Could not download slideshow image {index}: {short_error(exc)}",
            ))
            continue

        if not data:
            continue
        target.write_bytes(data)
        files.append(str(target))

    if not files:
        raise RuntimeError("None of the TikTok slideshow images could be downloaded.")

    queue.put(log_event(
        job_id, "info",
        f"TikTok slideshow: {len(files)}/{len(image_urls)} images downloaded.",
    ))

    return files, title, {
        "platform": "TikTok",
        "title": title,
        "uploader": uploader,
        "uploader_id": uploader,
        "webpage_url": url,
        "extractor": "tiktok:photo",
    }


# Trailing boilerplate yt-dlp appends to its error messages that carries no
# diagnostic value; stripped when the message is shortened.
_ERROR_BOILERPLATE = re.compile(
    r"\s*(?:Use --cookies-from-browser|Use --cookies |\. Also see |"
    r"For tips on how to effectively export|"
    r"for how to manually pass cookies|"
    r"for tips on effectively exporting YouTube cookies|"
    r"https://github\.com/yt-dlp/yt-dlp/wiki)"
    r".*$",
    re.IGNORECASE | re.DOTALL,
)


def short_error(error: Any, limit: int = 300) -> str:
    """
    Shortens an error message to something diagnosable.

    Cuts the message from the START, not the end. In yt-dlp's YouTube errors
    the real cause ("Sign in to confirm you're not a bot") comes first, and
    the wiki-link boilerplate comes last; cutting from the end used to strip
    exactly the useful part and keep only the boilerplate.
    """
    text = " ".join(str(error or "").split())
    text = _ERROR_BOILERPLATE.sub("", text).strip(" .")
    if not text:
        text = " ".join(str(error or "").split())[:limit]
    return text if len(text) <= limit else text[: limit - 1] + "…"


# The combination that plays cleanly on every device (especially macOS
# QuickTime/Safari and iOS). Anything else in an mp4 container triggers
# "unsupported codec" — the file opens but won't play.
_COMPATIBLE_VCODECS = {"h264", "avc1"}
_COMPATIBLE_ACODECS = {"aac", "mp4a"}


def _probe_streams(path: Path) -> tuple[str, str] | None:
    """Returns the video's (video_codec, audio_codec). None if unreadable."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    vcodec = acodec = ""
    for stream in streams:
        name = str(stream.get("codec_name") or "").lower()
        if stream.get("codec_type") == "video" and not vcodec:
            vcodec = name
        elif stream.get("codec_type") == "audio" and not acodec:
            acodec = name
    if not vcodec:
        return None
    return vcodec, acodec


def _has_faststart(path: Path) -> bool:
    """
    Is the moov atom before mdat in this mp4? If not, a player can't start
    playback without downloading the whole file (this is what made in-app
    playback on Telegram stall).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024 * 512)
    except OSError:
        return True  # can't read it, leave it alone
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1:
        return False
    return mdat == -1 or moov < mdat


def _ensure_playable(files: list[str], *, job_id: str, queue: Any) -> list[str]:
    """
    Makes downloaded videos playable on every device.

    The format selector already returns h264+aac in most cases; this layer
    only kicks in when it couldn't (the platform has no h264 variant at
    all). Compatible files are NEVER transcoded — transcoding a 28-second
    reel takes ~20s and doubles the file size. Compatible files missing
    faststart are only remuxed with stream-copy (sub-second).
    """
    if not shutil.which("ffmpeg"):
        return files

    result: list[str] = []
    for item in files:
        path = Path(item)
        if path.suffix.lower() not in VIDEO_EXTS:
            result.append(item)
            continue

        probed = _probe_streams(path)
        if probed is None:
            result.append(item)
            continue

        vcodec, acodec = probed
        video_ok = vcodec in _COMPATIBLE_VCODECS
        audio_ok = (not acodec) or acodec in _COMPATIBLE_ACODECS
        container_ok = path.suffix.lower() == ".mp4"

        if video_ok and audio_ok and container_ok and _has_faststart(path):
            result.append(item)
            continue

        target = path.with_name(path.stem + ".compat.mp4")
        # Map streams EXPLICITLY. ffmpeg's default picks only ONE of each
        # type, and without -c:s it drops embedded subtitles entirely — a
        # subtitled video used to lose its subtitles going through this
        # layer. The "?" suffix avoids an error when that stream is absent.
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
            "-c:s", "mov_text",
        ]
        if video_ok:
            command += ["-c:v", "copy"]
        else:
            command += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
            ]
        if audio_ok:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += ["-movflags", "+faststart", str(target)]

        what = "remuxing" if (video_ok and audio_ok) else \
            f"transcoding to h264/aac ({vcodec}/{acodec or 'no audio'})"
        queue.put(log_event(job_id, "info", f"Video compatibility: {path.name} {what}."))

        try:
            subprocess.run(command, capture_output=True, timeout=1800, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            # Sending the original beats sending nothing if the conversion fails.
            target.unlink(missing_ok=True)
            queue.put(log_event(
                job_id, "warning",
                f"Video compatibility conversion failed, sending the original: {short_error(exc)}",
            ))
            result.append(item)
            continue

        if not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            result.append(item)
            continue

        path.unlink(missing_ok=True)
        final = path.with_suffix(".mp4")
        if final.exists() and final != target:
            final.unlink(missing_ok=True)
        target.rename(final)
        result.append(str(final))

    return result


def _clear_partial_files(download_dir: Path) -> None:
    """
    Deletes files left behind by a failed attempt.

    Called after every failed attempt so half-finished .part/.ytdl files
    don't bleed into the next attempt's result.
    """
    try:
        for item in Path(download_dir).iterdir():
            try:
                if item.is_file():
                    item.unlink()
                else:
                    shutil.rmtree(item, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def _is_photo_only_error(error: Exception) -> bool:
    """
    Does the error say "no video in this post"?

    This isn't an access/session problem, it's the content type: the post is
    a photo or carousel. Changing cookies or the format profile won't fix it.
    """
    msg = str(error).lower()
    return (
        "there is no video in this post" in msg
        or "no video formats found" in msg
        or "no video could be found" in msg
    )


def _is_audience_gated_error(error: Exception) -> bool:
    """Instagram's wording for content limited to certain viewers.

    It is a gate on the *account* asking, not on the request: another cookie
    profile or a looser format selector returns the exact same answer, so the
    remaining attempts are pure rate-limit fuel.
    """
    msg = str(error).lower()
    return (
        "isn't available to everyone" in msg
        or "isn t available to everyone" in msg
        or "can't be seen by certain audiences" in msg
    )


def _should_retry_without_cookies(error: Exception) -> bool:
    msg = str(error).lower()
    markers = [
        "http error 403",
        "403: forbidden",
        "requested format is not available",
        "no video formats found",
        "sign in to confirm",
        "the following content is not available",
        "unable to download webpage",
    ]
    return any(marker in msg for marker in markers)


# ── Mode helpers ─────────────────────────────────────────────────────────────
# Supported download modes:
#   video_best / video_1080 / video_720 / video_480 / video_360
#   audio_best / audio_mp3 / audio_320 / audio_192 / audio_128 / audio_flac
#   thumbnail
#   auto / media_auto  (social platform / direct)

_AUDIO_QUALITY = {
    "audio_best": "320",
    "audio_mp3": "192",
    "audio_320": "320",
    "audio_192": "192",
    "audio_128": "128",
    "audio_flac": "0",  # lossless — no bitrate parameter
    "audio": "320",  # backward compatibility
}

# Which mode converts to which codec. Default is mp3.
#
# On FLAC: it's lossless, but if the source is already lossy (YouTube opus,
# Instagram/TikTok aac), converting to FLAC does NOT recover the lost
# information — it only makes the file 5-10x bigger. So FLAC is NOT the
# default, it's a separate option: useful when the source itself is lossless
# (e.g. Bandcamp) or for archiving.
_AUDIO_CODEC = {
    "audio_flac": "flac",
}

_VIDEO_HEIGHT = {
    "video_1080": 1080,
    "video_720": 720,
    "video_480": 480,
    "video_360": 360,
}


def _is_audio_mode(mode: str) -> bool:
    return mode.startswith("audio")


def _is_video_mode(mode: str) -> bool:
    return mode.startswith("video")


def _is_thumbnail_mode(mode: str) -> bool:
    return mode == "thumbnail"


def _reject_live_filter(info: dict, *, incomplete: bool = False) -> str | None:
    """
    yt-dlp match_filter: also filters out livestreams during the download
    itself.

    Catches a stream that slipped past the probe (e.g. it went live between
    the query and the download); yt-dlp never starts the download here.
    """
    if info_is_live(info):
        return "livestream — download skipped"
    return None


def _make_hook(job_id: str, queue: Any):
    last_progress_time = 0.0

    def hook(data: dict) -> None:
        nonlocal last_progress_time

        status = data.get("status")

        if status == "downloading":
            now = time.time()
            if now - last_progress_time < 1.0:
                return
            last_progress_time = now

            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes")
            percent = None

            if total and downloaded:
                percent = min(100.0, downloaded * 100.0 / total)

            queue.put(progress_event(
                job_id=job_id,
                percent=percent,
                downloaded=downloaded,
                total=total,
                speed=data.get("speed"),
                eta=data.get("eta"),
                status="downloading",
            ))

        elif status == "finished":
            queue.put(progress_event(
                job_id=job_id,
                percent=100.0,
                status="processing",
            ))

    return hook


def _build_opts(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    queue: Any,
    cookies_file: Path | None,
    mode: str,
    use_cookies: bool,
    format_profile: str,
    subtitle_lang: str = "",
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": _is_single_story(url) or not _is_social_url(url),
        "outtmpl": str(download_dir / "%(title).180B [%(id)s].%(ext)s"),
        "restrictfilenames": False,
        "windowsfilenames": False,
        "nopart": False,
        "retries": 3,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "http_chunk_size": 1024 * 1024 * 10,
        "http_headers": HTTP_HEADERS.copy(),
        "progress_hooks": [_make_hook(job_id, queue)],
        # ── Livestream / endless-stream protection ──
        # match_filter: live content never enters the download.
        # max_filesize: an unbounded stream stops before filling the disk.
        # wait_for_video: never WAIT for content that hasn't gone live yet
        # (an infinite wait was just another shape of the same lockup).
        "match_filter": _reject_live_filter,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "wait_for_video": None,
        "live_from_start": False,
    }

    if use_cookies and cookies_file and cookies_file.exists():
        opts["cookiefile"] = str(cookies_file)

    if shutil.which("ffmpeg"):
        opts["ffmpeg_location"] = shutil.which("ffmpeg")

    # ── Thumbnail mode ──────────────────────────────────────────────────────
    if _is_thumbnail_mode(mode):
        opts["skip_download"] = True
        opts["writethumbnail"] = True
        opts["write_all_thumbnails"] = False
        return opts

    # ── Audio modes ───────────────────────────────────────────────────────────
    if _is_audio_mode(mode):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found. It's required for audio downloads.")

        quality = _AUDIO_QUALITY.get(mode, "320")
        codec = _AUDIO_CODEC.get(mode, "mp3")

        # For FLAC, getting the source's best audio stream matters too: a
        # lossless container around a low-bitrate stream only inflates the
        # file, not the quality.
        opts["format"] = "bestaudio/best"
        if codec == "flac":
            opts["format_sort"] = ["abr", "asr"]

        extract: dict[str, Any] = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
        }
        # preferredquality is meaningless for FLAC; passing it sends ffmpeg
        # an invalid bitrate argument.
        if codec != "flac":
            extract["preferredquality"] = quality

        opts["postprocessors"] = [
            extract,
            # Let ffmpeg write the basic tags (title/artist/album). Without
            # this step the file only had an encoder tag.
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ]

        # The thumbnail is written to disk but EmbedThumbnail is NOT used: it
        # embeds the 16:9 video thumbnail as-is and the cover came out with
        # black bars. Cropping it to a square and embedding it happens in
        # bot/downloader/metadata.py after the download.
        opts["writethumbnail"] = True
        return opts

    if format_profile == "loose":
        return opts

    # ── Video modes (height cap) ──────────────────────────────────────────────
    height = _VIDEO_HEIGHT.get(mode)
    if height:
        opts["format"] = (
            f"bv*[height<={height}]+ba/b[height<={height}]/"
            f"best[height<={height}]/best"
        )
    elif _is_instagram_url(url):
        # Instagram's DASH streams are vp09 ONLY, but the numbered
        # (progressive) mp4 formats are h264+aac. So progressive is tried
        # first on Instagram — none of its fields (vcodec/ext/height) are
        # populated, so the only marker is "dash" being absent from
        # format_id.
        #
        # This branch is DELIBERATELY Instagram-only: applied generally it
        # made YouTube fall back to format 18 (640x360).
        opts["format"] = "b[ext=mp4][format_id!*=dash]/bv*+ba/b/best"
    elif _is_social_url(url):
        opts["format"] = "bv*+ba/b/best"
    else:
        # video_best and the general case
        opts["format"] = "bv*+ba/best/b"

    # ── Codec preference (macOS/iOS compatibility) ────────────────────────────
    # Without this, yt-dlp's default order is av01 > vp9 > h264, producing
    # av1+opus files in an .mp4 container that macOS QuickTime/Safari and iOS
    # refuse to play ("unsupported codec").
    #
    # Order matters:
    #   res:1080 — the best resolution NOT ABOVE 1080p. The cap is required
    #     because YouTube only has h264 up to 1080p; an unbounded "res" picks
    #     2160p vp9, which is both unplayable and forces a 4K transcode below.
    #   vcodec/acodec — h264+aac wins at the same resolution when available.
    opts["format_sort"] = ["res:1080", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]

    opts["merge_output_format"] = "mp4"

    # ── Subtitles ───────────────────────────────────────────────────────────
    # When subtitle_lang is set, subtitles are downloaded and BURNED into the
    # video (so a separate .srt doesn't arrive as a second file). "auto"
    # accepts auto-generated subtitles too.
    if subtitle_lang:
        langs = [x.strip() for x in subtitle_lang.split(",") if x.strip()]
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs or ["en"]
        opts["subtitlesformat"] = "srt/best"
        opts.setdefault("postprocessors", []).append({
            "key": "FFmpegEmbedSubtitle",
            "already_have_subtitle": False,
        })

    return opts


def _try_ytdlp_once(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    queue: Any,
    cookies_file: Path | None,
    mode: str,
    use_cookies: bool,
    format_profile: str,
    subtitle_lang: str = "",
) -> tuple[list[str], str, dict[str, Any]]:
    opts = _build_opts(
        job_id=job_id,
        url=url,
        download_dir=download_dir,
        queue=queue,
        cookies_file=cookies_file,
        mode=mode,
        use_cookies=use_cookies,
        format_profile=format_profile,
        subtitle_lang=subtitle_lang,
    )

    title = ""
    compact_info: dict[str, Any] = {"platform": platform_name(url), "webpage_url": url}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if isinstance(info, dict):
            # Catches content that went live between the query and the download.
            if info_is_live(info):
                raise LiveStreamError("Livestreams cannot be downloaded.")
            title = str(info.get("title") or "")
            compact_info = _compact_info(info, url)

    files = collect_files(download_dir, mode=mode)
    if not files:
        raise RuntimeError("No downloaded file found.")

    if not _is_audio_mode(mode) and not _is_thumbnail_mode(mode):
        files = _ensure_playable(files, job_id=job_id, queue=queue)

    # ── Audio metadata + square cover ─────────────────────────────────────────
    # On top of the basic tags ffmpeg wrote, mutagen writes the full source
    # info (artists[], album_artist, release year, track number).
    if _is_audio_mode(mode) and isinstance(info, dict):
        for audio_file in files:
            if Path(audio_file).suffix.lower() in AUDIO_EXTS:
                written = apply_audio_metadata(audio_file, info, job_id=job_id)
                if written:
                    queue.put(log_event(
                        job_id, "info",
                        "Metadata written: " + ", ".join(sorted(written)),
                    ))

    return files, title, compact_info


def download_with_ytdlp(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    queue: Any,
    cookies_file: Path | None = None,
    mode: str = "auto",
    allow_gallery_fallback: bool = True,
    skip_live_check: bool = False,
    subtitle_lang: str = "",
) -> tuple[list[str], str, dict[str, Any]]:
    _log_cookie_status(job_id, url, cookies_file, queue)

    # ── Livestream pre-check ──────────────────────────────────────────────────
    # Asked BEFORE the download. A livestream never ends; yt-dlp hands it off
    # to an ffmpeg subprocess and the download would never finish. Without
    # this the job slot would stay busy forever and the disk would keep
    # growing.
    # Spotify is handled in its own branch (yt-dlp can't resolve Spotify).
    # skip_live_check: skipped when the pipeline already did this check.
    if not skip_live_check and not _is_spotify_url(url):
        is_live, _probe = probe_is_live(url, cookies_file=cookies_file)
        if is_live:
            queue.put(log_event(job_id, "warning", f"Livestream rejected: {url}"))
            raise LiveStreamError("Livestreams cannot be downloaded.")

    errors: list[str] = []

    # ── Spotify: yt-dlp can't download it (DRM). Read metadata -> download audio from YouTube ──
    if _is_spotify_url(url):
        track = _spotify_track_info(url, queue, job_id)
        query = track["query"]
        search_url = "ytsearch1:" + query
        # Spotify always downloads as audio.
        spotify_mode = mode if _is_audio_mode(mode) else "audio_best"

        # The search goes to YouTube. The order is deliberately cookieless FIRST:
        #   • cookieless -> android client, DASH audio (3.2 MB for this track)
        #   • cookies    -> tv client, HLS only (the same track at 37 MB)
        # So the cookie-based attempt is both slower and ~10x more traffic.
        # But the cookieless attempt fails when YouTube says "Sign in to
        # confirm you're not a bot" (common from this VDS's IP); that used to
        # be the ONLY path, and Spotify became entirely unusable when it hit
        # that wall. The cookie-based attempt is the fallback for when the
        # cheap path is blocked.
        spotify_errors: list[str] = []
        for label, use_cookies in (("cookieless", False), ("cookies", True)):
            try:
                files, title, info = _try_ytdlp_once(
                    job_id=job_id,
                    url=search_url,
                    download_dir=download_dir,
                    queue=queue,
                    cookies_file=cookies_file,
                    mode=spotify_mode,
                    use_cookies=use_cookies,
                    format_profile="normal",
                )
                # Tag with Spotify's credits, not YouTube's.
                _apply_spotify_metadata(files, track, queue=queue, job_id=job_id)

                # Keep the original Spotify link as the source.
                info["platform"] = "Spotify"
                info["webpage_url"] = url
                info["title"] = track.get("title") or title or query
                if track.get("artist"):
                    info["artist"] = track["artist"]
                    info["uploader"] = track["artist"]
                return files, (track.get("title") or title or query), info

            except Exception as exc:
                message = short_error(exc)
                spotify_errors.append(f"{label}: {message}")
                queue.put(log_event(
                    job_id, "warning",
                    f"Spotify -> YouTube attempt failed [{label}]: {message}",
                ))
                _clear_partial_files(download_dir)

        raise RuntimeError(
            "Could not download the Spotify track through YouTube — "
            + " | ".join(spotify_errors)
        )

    # ── TikTok photo (slideshow) post ─────────────────────────────────────────
    # A short link (vt./vm.) doesn't say whether it's a photo or video post,
    # so it's resolved first. In audio mode there's no point downloading the
    # images: yt-dlp can already give the slideshow's music from /video/, so
    # this falls through to the normal flow.
    if _is_tiktok_url(url):
        resolved = _resolve_tiktok_url(url)
        photo_id = _tiktok_photo_id(resolved)
        if photo_id:
            video_url = resolved.replace("/photo/", "/video/", 1)
            if _is_audio_mode(mode) or _is_thumbnail_mode(mode):
                queue.put(log_event(
                    job_id, "info", "TikTok slideshow post — fetching audio from /video/.",
                ))
                url = video_url
            else:
                return _download_tiktok_photos(
                    job_id=job_id,
                    url=url,
                    video_url=video_url,
                    photo_id=photo_id,
                    download_dir=download_dir,
                    queue=queue,
                    cookies_file=cookies_file,
                )

    if _is_thumbnail_mode(mode):
        attempts = [
            ("cookies", True, "normal"),
            ("cookieless", False, "normal"),
        ]
    elif _is_audio_mode(mode):
        attempts = [
            ("cookies", True, "normal"),
            ("cookieless", False, "normal"),
        ]
    elif _is_social_url(url):
        attempts = [
            ("cookies", True, "normal"),
            ("cookieless", False, "normal"),
            ("cookies-loose", True, "loose"),
            ("cookieless-loose", False, "loose"),
        ]
    else:
        attempts = [
            ("cookies", True, "normal"),
            ("cookieless", False, "normal"),
        ]

    # ── Instagram: skip cookie-based attempts entirely on a locked session ──
    # On a checkpointed account, EVERY cookie-based request returns 400.
    # These aren't just wasted time; repeatedly poking a locked account is
    # bad too. Reels already come through the cookieless path, so dropping
    # the cookie-based attempts doesn't lose any downloadable content.
    if _is_instagram_url(url):
        ig_state = _instagram_session_state(cookies_file)
        if ig_state == "checkpoint":
            global _IG_CHECKPOINT_REPORTED
            queue.put(log_event(job_id, "error", IG_CHECKPOINT_MESSAGE))
            errors.append("instagram: checkpoint_required — " + IG_CHECKPOINT_MESSAGE)

            # Report to the panel. Done even if the download later succeeds
            # through the cookieless path: reels download fine but /p/ posts
            # and stories don't, so the lock is a malfunction the admin
            # should see regardless. Reporting only on outright failure would
            # let the lock go unnoticed while a whole content type silently
            # disappeared.
            if time.time() - _IG_CHECKPOINT_REPORTED > _IG_SESSION_TTL:
                _IG_CHECKPOINT_REPORTED = time.time()
                queue.put(cookie_event(
                    job_id=job_id,
                    platform="Instagram",
                    reason="account locked — verification required (refreshing cookies is not enough)",
                    url=url,
                    error="checkpoint_required — " + IG_CHECKPOINT_MESSAGE,
                ))

            filtered = [item for item in attempts if not item[1]]
            if filtered:
                attempts = filtered

        elif ig_state == "logged_out":
            # No session at all: a "cookies" attempt sends the same anonymous
            # request as the cookieless one, so running both only doubles the
            # requests Instagram counts against the rate limit.
            global _IG_LOGGED_OUT_REPORTED
            queue.put(log_event(job_id, "warning", IG_LOGGED_OUT_MESSAGE))

            if time.time() - _IG_LOGGED_OUT_REPORTED > _IG_SESSION_TTL:
                _IG_LOGGED_OUT_REPORTED = time.time()
                queue.put(cookie_event(
                    job_id=job_id,
                    platform="Instagram",
                    reason="no session — the sessionid cookie is missing or expired",
                    url=url,
                    error=IG_LOGGED_OUT_MESSAGE,
                ))

            filtered = [item for item in attempts if not item[1]]
            if filtered:
                attempts = filtered

    for label, use_cookies, format_profile in attempts:
        try:
            queue.put(log_event(job_id, "info", f"yt-dlp attempt: {label}"))
            return _try_ytdlp_once(
                job_id=job_id,
                url=url,
                download_dir=download_dir,
                queue=queue,
                cookies_file=cookies_file,
                mode=mode,
                use_cookies=use_cookies,
                format_profile=format_profile,
                subtitle_lang=subtitle_lang,
            )

        except LiveStreamError:
            # Livestream: no point retrying, exit right away.
            queue.put(log_event(job_id, "warning", f"Livestream rejected [{label}]: {url}"))
            raise

        except Exception as exc:
            message = short_error(exc)
            errors.append(f"{label}: {message}")
            queue.put(log_event(job_id, "warning", f"yt-dlp failed [{label}]: {message}"))

            # "No video in this post" is not an access issue, it's the
            # content type: a photo/carousel post. Neither cookies nor the
            # format profile change the outcome — this used to run 4
            # pointless attempts against Instagram photo posts, poking it
            # every time. Go straight to gallery-dl, the only source that
            # can fetch images.
            if _is_audience_gated_error(exc):
                queue.put(log_event(
                    job_id, "info",
                    "Instagram limits this post to certain viewers — "
                    "further attempts would return the same answer.",
                ))
                break

            if _is_photo_only_error(exc):
                queue.put(log_event(
                    job_id, "info",
                    "No video in this post (photo/carousel) — switching to gallery-dl for images.",
                ))
                break

            if not _should_retry_without_cookies(exc) and not _is_social_url(url):
                break

    if (
        allow_gallery_fallback
        and _is_social_url(url)
        and not _is_audio_mode(mode)
        and not _is_thumbnail_mode(mode)
    ):
        try:
            return _download_with_gallery_dl(
                job_id=job_id,
                url=url,
                download_dir=download_dir,
                cookies_file=cookies_file,
                queue=queue,
            )
        except Exception as exc:
            errors.append(f"gallery-dl: {short_error(exc)}")

    raise RuntimeError("Download failed. Attempts: " + " | ".join(errors[-5:]))
