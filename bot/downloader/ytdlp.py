from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yt_dlp

from bot.downloader.metadata import apply_audio_metadata
from bot.live_guard import info_is_live, probe_is_live
from bot.queue_events import log_event, progress_event
from bot.utils import platform_name


class LiveStreamError(RuntimeError):
    """Canlı yayın tespit edildi — indirme hiç başlatılmaz."""


# Tek bir indirmenin diskte kaplayabileceği üst sınır (bayt).
# Sonsuz akışlara karşı son savunma: yt-dlp bu sınırı aşınca durur.
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
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

# Sosyal platformlar: çoklu medya (carousel/slideshow) içerebilir, bu yüzden
# noplaylist kapatılır ve yt-dlp başarısız olursa gallery-dl denenir.
SOCIAL_PLATFORMS = {
    "Instagram",
    "TikTok",
    "Reddit",
    "Pinterest",
    "X/Twitter",
    "Facebook",
    # Faz 2: galeri/çoklu görsel içeriği olan platformlar
    "Tumblr",
    "Imgur",
    "Bluesky",
    "Snapchat",
}


def collect_files(download_dir: Path, *, mode: str = "auto") -> list[str]:
    files: list[str] = []

    # Thumbnail modunda yalnızca resim dosyaları toplanır.
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
    queue.put(log_event(job_id, "warning", "gallery-dl fallback deneniyor."))

    cmd = _gallery_command()
    cmd += ["-d", str(download_dir)]

    if cookies_file and cookies_file.exists():
        cmd += ["--cookies", str(cookies_file)]

    cmd.append(url)

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if stdout:
        queue.put(log_event(job_id, "info", "gallery-dl stdout: " + stdout[-700:]))

    if proc.returncode != 0:
        raise RuntimeError("gallery-dl başarısız: " + (stderr or stdout or "bilinmeyen hata")[-1000:])

    files = collect_files(download_dir)
    if not files:
        raise RuntimeError("gallery-dl dosya indirmedi.")

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


def _log_cookie_status(job_id: str, url: str, cookies_file: Path | None, queue: Any) -> None:
    if not cookies_file:
        queue.put(log_event(job_id, "warning", "Cookie dosyası ayarlı değil."))
        return

    if not cookies_file.exists():
        queue.put(log_event(job_id, "warning", f"Cookie dosyası yok: {cookies_file}"))
        return

    try:
        size = cookies_file.stat().st_size
    except OSError:
        size = 0

    queue.put(log_event(job_id, "info", f"Cookie dosyası aktif: {cookies_file} ({size} bytes)"))

    host = (urlparse(url).netloc or "").lower()

    if "facebook" in host or "fb.watch" in host:
        names = _cookie_names_for_domain(cookies_file, "facebook")
        safe_names = ", ".join(names[:20]) if names else "yok"
        queue.put(log_event(job_id, "info", f"Facebook cookie isimleri: {safe_names}"))

    if "tiktok" in host:
        names = _cookie_names_for_domain(cookies_file, "tiktok")
        safe_names = ", ".join(names[:20]) if names else "yok"
        queue.put(log_event(job_id, "info", f"TikTok cookie isimleri: {safe_names}"))


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


def _is_spotify_url(url: str) -> bool:
    return "spotify" in (urlparse(url).netloc or "").lower()


def _spotify_track_query(url: str, queue: Any, job_id: str) -> str:
    """
    Spotify track sayfasından şarkı adı + sanatçıyı okuyup
    YouTube araması için bir sorgu metni üretir.

    yt-dlp Spotify'dan ses indiremez (DRM). Bu yüzden spotdl mantığı uygulanır:
    Spotify metadata -> YouTube'da ara -> oradan ses indir.
    """
    path = (urlparse(url).path or "").lower()
    if "/track/" not in path:
        raise RuntimeError(
            "Spotify'dan yalnızca tekil şarkı (track) linkleri indirilebilir. "
            "Albüm/playlist linkleri desteklenmiyor."
        )

    # Spotify'ın oEmbed ucu API anahtarı gerektirmez ve şarkı başlığını verir.
    oembed = "https://open.spotify.com/oembed?url=" + url
    title = ""
    author = ""
    try:
        req = Request(oembed, headers=HTTP_HEADERS)
        with urlopen(req, timeout=20) as resp:
            import json as _json
            data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
        title = str(data.get("title") or "").strip()
        author = str(data.get("author_name") or "").strip()
    except Exception as exc:
        queue.put(log_event(job_id, "warning", f"Spotify oembed başarısız: {exc}"))

    # Yedek / tamamlayıcı: Spotify embed sayfası (JS gerektirmez) içindeki
    # gömülü JSON'dan başlık + sanatçıyı oku. oembed sanatçı vermez; sorgu
    # doğruluğu için sanatçı adı önemlidir.
    if not title or not author:
        track_id = ""
        m = re.search(r"/track/([A-Za-z0-9]+)", url)
        if m:
            track_id = m.group(1)
        if track_id:
            embed = f"https://open.spotify.com/embed/track/{track_id}"
            try:
                req = Request(embed, headers=HTTP_HEADERS)
                with urlopen(req, timeout=20) as resp:
                    page = resp.read().decode("utf-8", errors="ignore")
                if not author:
                    m = re.search(r'"artists":\[\{"name":"([^"]+)"', page)
                    if not m:
                        m = re.search(r'"name":"([^"]+)","uri":"spotify:artist', page)
                    if m:
                        author = html.unescape(m.group(1)).strip()
                if not title:
                    m = re.search(r'"name":"([^"]+)","uri":"spotify:track', page)
                    if not m:
                        m = re.search(r'"title":"([^"]+)"', page)
                    if m:
                        title = html.unescape(m.group(1)).strip()
            except Exception as exc:
                queue.put(log_event(job_id, "warning", f"Spotify embed okunamadı: {exc}"))

    if not title:
        raise RuntimeError("Spotify şarkı bilgisi alınamadı.")

    query = f"{title} {author}".strip()
    queue.put(log_event(job_id, "info", f"Spotify -> YouTube araması: {query}"))
    return query


def _is_tiktok_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "tiktok" in host


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


# ── Mode yardımcıları ────────────────────────────────────────────────────────
# Desteklenen indirme modları:
#   video_best / video_1080 / video_720 / video_480 / video_360
#   audio_best / audio_mp3 / audio_320 / audio_192 / audio_128
#   thumbnail
#   auto / media_auto  (sosyal platform / direkt)

_AUDIO_QUALITY = {
    "audio_best": "320",
    "audio_mp3": "192",
    "audio_320": "320",
    "audio_192": "192",
    "audio_128": "128",
    "audio": "320",  # geriye dönük uyumluluk
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
    yt-dlp match_filter: canlı yayınları indirme sırasında da eler.

    probe aşamasını atlatan bir yayın (ör. sorgu sırasında yayına başlayan
    içerik) buraya takılır; yt-dlp indirmeyi hiç başlatmaz.
    """
    if info_is_live(info):
        return "canlı yayın — indirme atlandı"
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
        "noplaylist": False if _is_social_url(url) else True,
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
        # ── Canlı yayın / sonsuz akış koruması ──
        # match_filter: canlı içerik indirmeye hiç girmez.
        # max_filesize: sınırsız büyüyen bir dosya diski doldurmadan durur.
        # wait_for_video: yayına başlamamış içerik için BEKLEME (sonsuz bekleme
        # tam olarak kilitlenmenin bir başka biçimiydi).
        "match_filter": _reject_live_filter,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "wait_for_video": None,
        "live_from_start": False,
    }

    if use_cookies and cookies_file and cookies_file.exists():
        opts["cookiefile"] = str(cookies_file)

    if shutil.which("ffmpeg"):
        opts["ffmpeg_location"] = shutil.which("ffmpeg")

    # ── Thumbnail (kapak) modu ────────────────────────────────────────────────
    if _is_thumbnail_mode(mode):
        opts["skip_download"] = True
        opts["writethumbnail"] = True
        opts["write_all_thumbnails"] = False
        return opts

    # ── Ses modları ───────────────────────────────────────────────────────────
    if _is_audio_mode(mode):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg bulunamadı. Ses indirmek için ffmpeg gerekli.")

        quality = _AUDIO_QUALITY.get(mode, "320")
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            },
            # Temel etiketleri ffmpeg yazsın (başlık/sanatçı/albüm).
            # Bu adım OLMADAN dosyada yalnızca kodlayıcı etiketi kalıyordu.
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ]

        # Küçük resim diske yazılır ama EmbedThumbnail KULLANILMAZ:
        # o, 16:9 video küçük resmini olduğu gibi gömüyor ve kapak siyah
        # bantlı çıkıyordu. Kapağı kareye kırpıp gömme işini
        # bot/downloader/metadata.py yapıyor (indirmeden sonra).
        opts["writethumbnail"] = True
        return opts

    if format_profile == "loose":
        return opts

    # ── Video modları (yükseklik sınırı) ──────────────────────────────────────
    height = _VIDEO_HEIGHT.get(mode)
    if height:
        opts["format"] = (
            f"bv*[height<={height}]+ba/b[height<={height}]/"
            f"best[height<={height}]/best"
        )
    elif _is_social_url(url):
        opts["format"] = "bv*+ba/b/best"
    else:
        # video_best ve diğer genel durumlar
        opts["format"] = "bv*+ba/best/b"

    opts["merge_output_format"] = "mp4"

    # ── Altyazı ───────────────────────────────────────────────────────────────
    # subtitle_lang verilirse altyazı indirilir ve videoya GÖMÜLÜR (ayrı .srt
    # dosyası Telegram'da ikinci bir dosya olarak gitmesin diye). "auto" ise
    # otomatik üretilen altyazılar da kabul edilir.
    if subtitle_lang:
        langs = [x.strip() for x in subtitle_lang.split(",") if x.strip()]
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs or ["tr"]
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
            # Sorgu ile indirme arasında yayına geçen içerik burada yakalanır.
            if info_is_live(info):
                raise LiveStreamError("Canlı yayınlar indirilemez.")
            title = str(info.get("title") or "")
            compact_info = _compact_info(info, url)

    files = collect_files(download_dir, mode=mode)
    if not files:
        raise RuntimeError("İndirilen dosya bulunamadı.")

    # ── Ses metadata'sı + kare kapak ──────────────────────────────────────────
    # ffmpeg'in yazdığı temel etiketlerin üzerine, kaynaktaki tam bilgiyi
    # (artists[], album_artist, yayın yılı, parça no) mutagen ile yazıyoruz.
    if _is_audio_mode(mode) and isinstance(info, dict):
        for audio_file in files:
            if Path(audio_file).suffix.lower() in AUDIO_EXTS:
                written = apply_audio_metadata(audio_file, info, job_id=job_id)
                if written:
                    queue.put(log_event(
                        job_id, "info",
                        "Metadata yazıldı: " + ", ".join(sorted(written)),
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

    # ── Canlı yayın ön kontrolü ───────────────────────────────────────────────
    # İndirmeden ÖNCE sorulur. Canlı yayın sonsuz akar; yt-dlp bunu bir ffmpeg
    # alt sürecine devreder ve indirme asla bitmez. Burada durdurulmazsa iş
    # slotu süresiz dolu kalır ve disk sürekli büyür.
    # Spotify kendi dalında ele alınır (yt-dlp Spotify'ı çözemez).
    # skip_live_check: pipeline bu kontrolü zaten yaptıysa tekrarlanmaz.
    if not skip_live_check and not _is_spotify_url(url):
        is_live, _probe = probe_is_live(url, cookies_file=cookies_file)
        if is_live:
            queue.put(log_event(job_id, "warning", f"Canlı yayın reddedildi: {url}"))
            raise LiveStreamError("Canlı yayınlar indirilemez.")

    errors: list[str] = []

    # ── Spotify: yt-dlp indiremez (DRM). Metadata oku -> YouTube'dan ses indir ──
    if _is_spotify_url(url):
        query = _spotify_track_query(url, queue, job_id)
        search_url = "ytsearch1:" + query
        # Spotify her zaman ses olarak indirilir.
        spotify_mode = mode if _is_audio_mode(mode) else "audio_best"
        try:
            files, title, info = _try_ytdlp_once(
                job_id=job_id,
                url=search_url,
                download_dir=download_dir,
                queue=queue,
                cookies_file=cookies_file,
                mode=spotify_mode,
                use_cookies=False,
                format_profile="normal",
            )
            # Kaynak olarak orijinal Spotify linkini koru.
            info["platform"] = "Spotify"
            info["webpage_url"] = url
            return files, (title or query), info
        except Exception as exc:
            raise RuntimeError(
                "Spotify şarkısı YouTube üzerinden indirilemedi: " + str(exc)[-200:]
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

    for label, use_cookies, format_profile in attempts:
        try:
            queue.put(log_event(job_id, "info", f"yt-dlp denemesi: {label}"))
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
            # Canlı yayın: yeniden denemenin anlamı yok, hemen çık.
            queue.put(log_event(job_id, "warning", f"Canlı yayın reddedildi [{label}]: {url}"))
            raise

        except Exception as exc:
            errors.append(f"{label}: {str(exc)[-300:]}")
            queue.put(log_event(job_id, "warning", f"yt-dlp başarısız [{label}]: {str(exc)[-350:]}"))

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
            errors.append(f"gallery-dl: {str(exc)[-300:]}")

    raise RuntimeError("İndirme başarısız. Denemeler: " + " | ".join(errors[-5:]))
