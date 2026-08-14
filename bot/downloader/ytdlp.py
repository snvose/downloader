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

    # gallery-dl'in getirdiği videolar da aynı uyumluluk katmanından geçer;
    # bu dal atlandığında yedek yolla gelen dosyalar hâlâ vp9/av1 kalıyordu.
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


def _is_instagram_url(url: str) -> bool:
    return "instagram" in (urlparse(url).netloc or "").lower()


# Instagram oturum durumu, TTL'li önbellek: (zaman, durum)
# Her iş için ağ isteği atmamak ve kilitli bir hesabı sürekli dürtmemek için.
_IG_SESSION_CACHE: tuple[float, str] = (0.0, "")
_IG_SESSION_TTL = 15 * 60
# Kilit bildirimi panele en fazla bu aralıkta bir düşer (sayaç şişmesin).
_IG_CHECKPOINT_REPORTED = 0.0

IG_CHECKPOINT_MESSAGE = (
    "Instagram hesabı kilitli (checkpoint_required) — cookie'nin sahibi hesap "
    "instagram.com/challenge/ adresinde doğrulama bekliyor. Doğrulama insan "
    "tarafından tamamlanıp cookie yeniden dışa aktarılmadan cookie'li "
    "Instagram indirmeleri çalışmaz."
)


def _instagram_session_state(cookies_file: Path | None) -> str:
    """
    cookies.txt'teki Instagram oturumunun durumunu döndürür.

    "ok"          — oturum çalışıyor
    "checkpoint"  — hesap kilitli, Instagram doğrulama istiyor
    "logged_out"  — oturum tanınmıyor (giriş sayfasına yönleniyor)
    "unknown"     — belirlenemedi (ağ hatası, cookie yok vb.)

    NEDEN GEREKLİ: kilitli hesapta yt-dlp'nin gördüğü tek şey
    "HTTP Error 400: Bad Request"; asıl sebep yanıt GÖVDESİNDE duruyor ve
    yt-dlp onu hata metnine koymuyor. Gövde okunmadan bu durum, sıradan bir
    ağ hatasından ayırt edilemiyordu — admin panelinde hiçbir uyarı çıkmıyor,
    kullanıcı da "HTTP Error 400" görüyordu.
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
        # feed/timeline/ oturum gerektiriyor, medya kimliği istemiyor ve
        # kilitli hesapta checkpoint gövdesini doğrudan döndürüyor.
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
    Spotify embed sayfasındaki __NEXT_DATA__ JSON'ını okur.

    Bu sayfa API anahtarı istemez ve şarkının GERÇEK künyesini verir:
    ad, sanatçı(lar), yayın tarihi ve 640x640 albüm kapağı. YouTube'dan
    gelen kanal adı / yükleme tarihi / video küçük resminin aksine bunlar
    şarkının kendi bilgileri.
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
    """Embed verisindeki en büyük albüm kapağını seçer."""
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
    Spotify track sayfasından şarkının künyesini okur.

    yt-dlp Spotify'dan ses indiremez (DRM). Bu yüzden spotdl mantığı uygulanır:
    Spotify metadata -> YouTube'da ara -> oradan ses indir.

    Dönüş: {"query", "title", "artist", "release_date", "cover_url"}
    Önceden yalnızca arama metni ("query") döndürülüyordu; geri kalan künye
    okunup atılıyordu ve dosyaya YouTube'un verisi yazılıyordu (sanatçı
    yerine kanal adı "Hidra Official", yıl yerine video yükleme tarihi,
    tür yerine YouTube kategorisi "Entertainment"). Artık kaynak künye
    korunuyor.
    """
    path = (urlparse(url).path or "").lower()
    if "/track/" not in path:
        raise RuntimeError(
            "Spotify'dan yalnızca tekil şarkı (track) linkleri indirilebilir. "
            "Albüm/playlist linkleri desteklenmiyor."
        )

    title = ""
    author = ""
    release_date = ""
    cover_url = ""
    page = ""

    # ── 1. Tercih edilen kaynak: embed sayfasının yapılandırılmış JSON'ı ──
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
        queue.put(log_event(job_id, "warning", f"Spotify embed okunamadı: {exc}"))

    # ── 2. Yedek: oEmbed ucu (başlık verir, sanatçı vermez) ──
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
            queue.put(log_event(job_id, "warning", f"Spotify oembed başarısız: {exc}"))

    # ── 3. Son çare: embed sayfasının ham HTML'inde regex ──
    # __NEXT_DATA__ yapısı değişirse akış tümden durmasın diye duruyor.
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
        raise RuntimeError("Spotify şarkı bilgisi alınamadı.")

    query = f"{title} {author}".strip()
    queue.put(log_event(job_id, "info", f"Spotify -> YouTube araması: {query}"))

    return {
        "query": query,
        "title": title,
        "artist": author,
        "release_date": release_date,
        "cover_url": cover_url,
    }


def _strip_youtube_tags(path: Path) -> None:
    """
    ffmpeg'in YouTube verisinden yazdığı, şarkıya ait OLMAYAN etiketleri siler.

    FFmpegMetadata son işlemcisi video sayfasından ne bulursa yazıyor:
    TCON'a YouTube kategorisi ("Entertainment"), yoruma video linki,
    açıklamaya kanalın sosyal medya listesi, TDRC'ye video yükleme tarihi.
    Bir şarkı dosyasında bunların hepsi yanlış bilgi. Doğrusu Spotify'dan
    geliyor; gelmeyen alan boş bırakılır, uydurulmaz.
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
    İndirilen sesin etiketlerini Spotify künyesiyle DEĞİŞTİRİR.

    Ses YouTube'dan geldiği için yt-dlp/ffmpeg oraya YouTube'un verisini
    yazıyor: sanatçı yerine kanal adı, yıl yerine video yükleme tarihi,
    tür yerine YouTube kategorisi, başlık yerine video başlığı. Kullanıcı
    Spotify linki gönderdiğine göre doğru künye Spotify'ınki.

    Albüm kapağı da Spotify'ın 640x640 kare kapağıyla değiştirilir; YouTube
    küçük resmi 16:9 video karesi olduğu için kırpılınca albüm kapağı gibi
    durmuyor.
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

        # Spotify kapağını ses dosyasının yanına .jpg olarak yaz; metadata
        # katmanı küçük resmi orada arıyor ve .jpg'yi .webp'den önce görüyor.
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
                    job_id, "warning", f"Spotify kapağı indirilemedi: {exc}"
                ))

        written = apply_audio_metadata(path, info, job_id=job_id)
        if written:
            queue.put(log_event(
                job_id, "info",
                "Spotify künyesi yazıldı: " + ", ".join(sorted(written)),
            ))


def _is_tiktok_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return "tiktok" in host


# ── TikTok slayt (fotoğraf) gönderileri ──────────────────────────────────────
# TikTok'ta iki tür gönderi var: /video/ ve /photo/. yt-dlp yalnızca ilkini
# tanıyor; TikTokIE._VALID_URL /photo/ ile HİÇ eşleşmiyor, extractor'da
# imagePost alanını okuyan tek satır bile yok. Sonuç: fotoğraf gönderisi
# "Unsupported URL" ile düşüyor, gallery-dl de adresi /video/'ya çevirip
# 403 yiyor (TikTok'un JS challenge'ını çözemiyor). Yani slayt gönderileri
# botta tamamen indirilemez durumdaydı.
#
# Çözüm: adresi /video/'ya çevirip sayfayı yt-dlp'ye çektiriyoruz — challenge
# çözme kodu zaten onda var ve çalışıyor. Görseller o adımın ürettiği ham
# veride (imagePost.images) duruyor, biz onu okuyup indiriyoruz.

_TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}


def _resolve_tiktok_url(url: str) -> str:
    """
    vt./vm. kısa linkini gerçek adrese çevirir.

    Kısa link fotoğraf mı video mu belli etmiyor; hangi dalda ilerleyeceğimizi
    ancak yönlendirmeyi izleyerek bilebiliyoruz.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    is_short = host in _TIKTOK_SHORT_HOSTS or (
        host.endswith("tiktok.com") and parsed.path.startswith("/t/")
    )
    if not is_short:
        return url

    # HEAD yeter: yalnızca yönlendirmenin bittiği adres lazım, sayfanın
    # kendisi değil (her kısa linkte ~400 KB gövde indirmenin anlamı yok).
    try:
        with urlopen(Request(url, headers=HTTP_HEADERS, method="HEAD"), timeout=15) as resp:
            return resp.url or url
    except Exception:
        return url


def _tiktok_photo_id(url: str) -> str:
    """Slayt gönderisiyse gönderi id'sini, değilse boş string döner."""
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
    Slayt gönderisinin ham TikTok verisini döner.

    yt-dlp'nin iç metodu çağrılıyor; sürüm yükseltmesinde adı değişirse
    indirme çökmesin diye AttributeError ayrıca ele alınıyor.
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
                "yt-dlp'nin TikTok iç arayüzü değişmiş, slayt gönderisi "
                f"okunamadı: {exc}"
            ) from exc

    return detail if isinstance(detail, dict) else {}


def _tiktok_photo_urls(detail: dict[str, Any]) -> list[str]:
    """imagePost.images[] içinden görsel adreslerini sırasıyla çıkarır."""
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
    """Slayt gönderisinin görsellerini indirir."""
    errors: list[str] = []
    detail: dict[str, Any] = {}

    for label, use_cookies in (("cookies", True), ("cookieless", False)):
        try:
            queue.put(log_event(job_id, "info", f"TikTok slayt denemesi: {label}"))
            detail = _tiktok_photo_detail(
                video_url, photo_id,
                cookies_file=cookies_file, use_cookies=use_cookies,
                queue=queue, job_id=job_id,
            )
            if _tiktok_photo_urls(detail):
                break
            errors.append(f"{label}: gönderide görsel bulunamadı")
        except Exception as exc:
            message = short_error(exc)
            errors.append(f"{label}: {message}")
            queue.put(log_event(
                job_id, "warning", f"TikTok slayt denemesi başarısız [{label}]: {message}",
            ))

    image_urls = _tiktok_photo_urls(detail)
    if not image_urls:
        raise RuntimeError(
            "TikTok slayt gönderisinin görselleri okunamadı — " + " | ".join(errors)
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
                f"Slayt görseli {index} indirilemedi: {short_error(exc)}",
            ))
            continue

        if not data:
            continue
        target.write_bytes(data)
        files.append(str(target))

    if not files:
        raise RuntimeError("TikTok slayt görsellerinin hiçbiri indirilemedi.")

    queue.put(log_event(
        job_id, "info",
        f"TikTok slayt gönderisi: {len(files)}/{len(image_urls)} görsel indirildi.",
    ))

    return files, title, {
        "platform": "TikTok",
        "title": title,
        "uploader": uploader,
        "uploader_id": uploader,
        "webpage_url": url,
        "extractor": "tiktok:photo",
    }


# yt-dlp hata mesajlarının sonuna eklediği, teşhis için değeri olmayan
# yönlendirme metinleri. Mesaj kısaltılırken bunlar atılır.
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
    Hata mesajını teşhis edilebilir biçimde kısaltır.

    Mesajı SONDAN değil BAŞTAN keser. yt-dlp'nin YouTube hatalarında asıl
    sebep ("Sign in to confirm you're not a bot") en başta, wiki linki
    içeren yönlendirme metni ise en sonda durur; sondan kesmek tam olarak
    işe yarayan kısmı atıp geriye yalnızca boilerplate bırakıyordu.
    """
    text = " ".join(str(error or "").split())
    text = _ERROR_BOILERPLATE.sub("", text).strip(" .")
    if not text:
        text = " ".join(str(error or "").split())[:limit]
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Her cihazda (özellikle macOS QuickTime/Safari ve iOS) sorunsuz açılan
# birleşim. Bunların dışındaki her şey mp4 kabında "codec desteklenmiyor"
# hatası veriyor: dosya açılıyor ama oynatılmıyor.
_COMPATIBLE_VCODECS = {"h264", "avc1"}
_COMPATIBLE_ACODECS = {"aac", "mp4a"}


def _probe_streams(path: Path) -> tuple[str, str] | None:
    """Videonun (video_codec, ses_codec) çiftini döndürür. Okunamazsa None."""
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
    mp4'te moov atomu mdat'tan önce mi? Değilse oynatıcı dosyanın tamamını
    indirmeden başlatamıyor (Telegram'da içeriden oynatma böyle takılıyordu).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024 * 512)
    except OSError:
        return True  # okuyamıyorsak dokunma
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1:
        return False
    return mdat == -1 or moov < mdat


def _ensure_playable(files: list[str], *, job_id: str, queue: Any) -> list[str]:
    """
    İndirilen videoları her cihazda oynatılabilir hale getirir.

    Format seçici çoğu durumda h264+aac'ı zaten getiriyor; bu katman yalnızca
    getiremediğinde (platformda h264 sürümü hiç yoksa) devreye giren son çare.
    Uyumlu dosyalarda transcode YAPILMAZ — 28 sn'lik bir reel'de transcode
    ~20 sn sürüyor ve dosyayı iki katına çıkarıyor. Uyumlu ama faststart'sız
    dosyalar yalnızca stream-copy ile yeniden paketlenir (saniyenin altında).
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

        target = path.with_name(path.stem + ".uyumlu.mp4")
        command = ["ffmpeg", "-y", "-v", "error", "-i", str(path)]
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

        what = "yeniden paketleniyor" if (video_ok and audio_ok) else \
            f"h264/aac'ye dönüştürülüyor ({vcodec}/{acodec or 'ses yok'})"
        queue.put(log_event(job_id, "info", f"Video uyumluluk: {path.name} {what}."))

        try:
            subprocess.run(command, capture_output=True, timeout=1800, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            # Dönüştürme başarısızsa orijinali göndermek, hiç göndermemekten iyi.
            target.unlink(missing_ok=True)
            queue.put(log_event(
                job_id, "warning",
                f"Video uyumluluk dönüşümü başarısız, orijinal gönderiliyor: {short_error(exc)}",
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
    Başarısız denemeden kalan dosyaları siler.

    Yarım kalan .part/.ytdl dosyaları sıradaki denemenin sonucuna
    karışmasın diye her başarısız denemeden sonra çağrılır.
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
    elif _is_instagram_url(url):
        # Instagram'ın DASH akışları YALNIZCA vp09 sunuyor; ama numaralı
        # (progressive) mp4 formatları h264+aac. Bu yüzden Instagram'da önce
        # progressive denenir — hiçbir alanı (vcodec/ext/height) dolu olmadığı
        # için codec süzgeciyle hedeflenemiyor, tek ayırt edici işaret
        # format_id'de "dash" geçmemesi.
        #
        # Bu dal KASITLI olarak yalnızca Instagram'a özel: genele konulduğunda
        # YouTube'da format 18'e (640x360) düşüyordu.
        opts["format"] = "b[ext=mp4][format_id!*=dash]/bv*+ba/b/best"
    elif _is_social_url(url):
        opts["format"] = "bv*+ba/b/best"
    else:
        # video_best ve diğer genel durumlar
        opts["format"] = "bv*+ba/best/b"

    # ── Codec tercihi (macOS/iOS uyumluluğu) ──────────────────────────────────
    # Bu düzeltme olmadan yt-dlp'nin öntanımlı sıralaması av01 > vp9 > h264
    # diyordu; .mp4 kabında av1+opus dosyalar üretiliyor ve macOS
    # QuickTime/Safari ile iOS "codec desteklenmiyor" deyip oynatmıyordu.
    #
    # Sıra önemli:
    #   res:1080 — 1080p'yi AŞMAYAN en iyi çözünürlük. Üst sınır şart, çünkü
    #     YouTube'da h264 yalnızca 1080p'ye kadar var; sınırsız "res" dendiğinde
    #     2160p vp9 kazanıyor ve dosya hem oynatılamıyor hem de aşağıdaki
    #     ffmpeg katmanına 4K transcode yaptırıyordu.
    #   vcodec/acodec — aynı çözünürlükte h264+aac varsa o seçilir.
    opts["format_sort"] = ["res:1080", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]

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

    if not _is_audio_mode(mode) and not _is_thumbnail_mode(mode):
        files = _ensure_playable(files, job_id=job_id, queue=queue)

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
        track = _spotify_track_info(url, queue, job_id)
        query = track["query"]
        search_url = "ytsearch1:" + query
        # Spotify her zaman ses olarak indirilir.
        spotify_mode = mode if _is_audio_mode(mode) else "audio_best"

        # Arama YouTube'a gider. Sıra kasıtlı olarak ÖNCE cookie'siz:
        #   • cookie'siz  -> android istemcisi, DASH ses (bu şarkıda 3.2 MB)
        #   • cookie'li   -> tv istemcisi, yalnızca HLS (aynı şarkı 37 MB)
        # Yani cookie'li deneme hem daha yavaş hem ~10x daha fazla trafik.
        # Ama cookie'siz deneme YouTube "Sign in to confirm you're not a bot"
        # dediğinde (VDS IP'sinde sık) başarısız oluyor; önceden TEK yol
        # oydu ve o anda Spotify tamamen kullanılamaz hale geliyordu.
        # Bu yüzden cookie'li deneme ucuz yol tıkandığında devreye giren
        # yedek olarak duruyor.
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
                # Etiketleri YouTube'unkiyle değil Spotify künyesiyle yaz.
                _apply_spotify_metadata(files, track, queue=queue, job_id=job_id)

                # Kaynak olarak orijinal Spotify linkini koru.
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
                    f"Spotify -> YouTube denemesi başarısız [{label}]: {message}",
                ))
                _clear_partial_files(download_dir)

        raise RuntimeError(
            "Spotify şarkısı YouTube üzerinden indirilemedi — "
            + " | ".join(spotify_errors)
        )

    # ── TikTok slayt (fotoğraf) gönderisi ─────────────────────────────────────
    # Kısa link (vt./vm.) fotoğraf mı video mu belli etmediği için önce çözülür.
    # Ses modunda görselleri indirmenin anlamı yok: yt-dlp /video/ adresinden
    # slaytın müziğini zaten verebiliyor, o yüzden normal akışa devredilir.
    if _is_tiktok_url(url):
        resolved = _resolve_tiktok_url(url)
        photo_id = _tiktok_photo_id(resolved)
        if photo_id:
            video_url = resolved.replace("/photo/", "/video/", 1)
            if _is_audio_mode(mode) or _is_thumbnail_mode(mode):
                queue.put(log_event(
                    job_id, "info", "TikTok slayt gönderisi — ses /video/ adresinden alınıyor.",
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

    # ── Instagram: kilitli oturumda cookie'li denemeleri hiç yapma ────────────
    # Hesap checkpoint'e düştüğünde cookie'li HER istek 400 dönüyor. Bunlar
    # sadece boşa zaman değil; kilitli bir hesabı arka arkaya dürtmek de
    # kötü. Reel'ler zaten cookie'siz yoldan iniyor, o yüzden cookie'li
    # denemeleri elemek indirilebilir içeriği kaybettirmiyor.
    if _is_instagram_url(url):
        ig_state = _instagram_session_state(cookies_file)
        if ig_state == "checkpoint":
            global _IG_CHECKPOINT_REPORTED
            queue.put(log_event(job_id, "error", IG_CHECKPOINT_MESSAGE))
            errors.append("instagram: checkpoint_required — " + IG_CHECKPOINT_MESSAGE)

            # Panele bildir. Bu, indirme sonradan cookie'siz yoldan BAŞARILI
            # olsa bile yapılır: reel'ler cookie'siz iniyor ama /p/ gönderileri
            # ve story'ler inmiyor, yani kilit her hâlükârda admin'in görmesi
            # gereken bir arıza. Yalnızca hata anında raporlansaydı, kilit
            # görünmez kalıp bütün bir gönderi türü sessizce kaybolacaktı.
            if time.time() - _IG_CHECKPOINT_REPORTED > _IG_SESSION_TTL:
                _IG_CHECKPOINT_REPORTED = time.time()
                queue.put(cookie_event(
                    job_id=job_id,
                    platform="Instagram",
                    reason="hesap kilitli — doğrulama gerekiyor (cookie yenilemek yetmez)",
                    url=url,
                    error="checkpoint_required — " + IG_CHECKPOINT_MESSAGE,
                ))

            filtered = [item for item in attempts if not item[1]]
            if filtered:
                attempts = filtered

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
            message = short_error(exc)
            errors.append(f"{label}: {message}")
            queue.put(log_event(job_id, "warning", f"yt-dlp başarısız [{label}]: {message}"))

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

    raise RuntimeError("İndirme başarısız. Denemeler: " + " | ".join(errors[-5:]))
