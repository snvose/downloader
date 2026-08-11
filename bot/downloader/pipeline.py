from __future__ import annotations

"""
bot/downloader/pipeline.py — çok kaynaklı indirme akışı.

Bir linki, platforma göre belirlenmiş sırayla birden fazla kaynakta dener:
    cobalt → yt-dlp → gallery-dl   (sıra data/sources.json ile ayarlanır)

Bir kaynak başarısız olursa sıradaki denenir; hepsi başarısız olursa
toplu hata mesajı döner. Canlı yayın kontrolü kaynaklardan ÖNCE, tek
seferde yapılır (bkz. bot/live_guard.py).
"""

import time
from pathlib import Path
from typing import Any

from bot.cookie_health import classify_cookie_error, error_platform_hint
from bot.live_guard import probe_is_live
from bot.queue_events import cookie_event, log_event, progress_event
from bot.utils import platform_name

from .cobalt import CobaltClient, CobaltError, CobaltUnavailable, platform_supported
from .sources import SourcePriority
from .ytdlp import (
    LiveStreamError,
    _clear_partial_files as _clear_partial,
    _download_with_gallery_dl,
    _is_spotify_url,
    collect_files,
    download_with_ytdlp,
    short_error,
)


def _cobalt_progress(job_id: str, queue: Any):
    """cobalt indirme ilerlemesini bot kuyruğuna aktarır (saniyede bir)."""
    last = [0.0]

    def hook(written: int, total: int) -> None:
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        percent = (written * 100.0 / total) if total else None
        queue.put(progress_event(
            job_id=job_id,
            percent=percent,
            downloaded=written,
            total=total or None,
            status="downloading",
        ))

    return hook


def _run_cobalt(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    queue: Any,
    mode: str,
    client: CobaltClient,
    platform: str,
    subtitle_lang: str = "",
) -> tuple[list[str], str, dict[str, Any]]:
    if not client.enabled:
        raise CobaltUnavailable("cobalt yapılandırılmamış.")

    if not platform_supported(platform):
        raise CobaltUnavailable(f"cobalt bu platformu desteklemiyor: {platform}")

    files, cinfo = client.download(
        url=url,
        download_dir=download_dir,
        mode=mode,
        subtitle_lang=subtitle_lang,
        on_progress=_cobalt_progress(job_id, queue),
    )

    if not files:
        raise CobaltError("cobalt dosya döndürmedi.")

    title = Path(files[0]).stem
    info: dict[str, Any] = {
        "platform": platform,
        "title": title,
        "webpage_url": url,
        "description": "",
        "source": "cobalt",
        "cobalt_status": cinfo.get("cobalt_status", ""),
    }
    return files, title, info


def download_media(
    *,
    job_id: str,
    url: str,
    download_dir: Path,
    queue: Any,
    cookies_file: Path | None = None,
    mode: str = "auto",
    priority: SourcePriority | None = None,
    cobalt: CobaltClient | None = None,
    subtitle_lang: str = "",
) -> tuple[list[str], str, dict[str, Any]]:
    """
    Linki, platforma göre sıralanmış kaynaklarda dener ve ilk başarılı
    sonucu döner.

    LiveStreamError yukarı iletilir — canlı yayın hiçbir kaynakta denenmez.
    """
    platform = platform_name(url)
    download_dir = Path(download_dir)

    # ── Canlı yayın kontrolü (tek sefer, tüm kaynaklar için) ──────────────────
    if not _is_spotify_url(url):
        is_live, _probe = probe_is_live(url, cookies_file=cookies_file)
        if is_live:
            queue.put(log_event(job_id, "warning", f"Canlı yayın reddedildi: {url}"))
            raise LiveStreamError("Canlı yayınlar indirilemez.")

    # ── Kullanılabilir kaynaklar ──────────────────────────────────────────────
    available = {"ytdlp", "gallerydl"}
    if cobalt and cobalt.enabled and platform_supported(platform):
        available.add("cobalt")

    # Spotify özel akışı yalnızca yt-dlp'de var (metadata → YouTube araması).
    if _is_spotify_url(url):
        order = ["ytdlp"]
    else:
        priority = priority or SourcePriority(download_dir.parent.parent)
        order = priority.for_platform(platform, available=available)

    queue.put(log_event(job_id, "info", f"Kaynak sırası [{platform}]: {' → '.join(order)}"))

    errors: list[str] = []

    for source in order:
        try:
            queue.put(log_event(job_id, "info", f"Kaynak deneniyor: {source}"))

            if source == "cobalt":
                files, title, info = _run_cobalt(
                    job_id=job_id, url=url, download_dir=download_dir,
                    queue=queue, mode=mode, client=cobalt, platform=platform,
                    subtitle_lang=subtitle_lang,
                )

            elif source == "ytdlp":
                # gallery-dl fallback'i pipeline yönetir; yt-dlp içinde tekrar
                # denenmesin (aynı kaynak iki kez çalışmasın).
                files, title, info = download_with_ytdlp(
                    job_id=job_id, url=url, download_dir=download_dir,
                    queue=queue, cookies_file=cookies_file, mode=mode,
                    allow_gallery_fallback=False,
                    skip_live_check=True,  # yukarıda zaten yapıldı
                    subtitle_lang=subtitle_lang,
                )

            elif source == "gallerydl":
                files, title, info = _download_with_gallery_dl(
                    job_id=job_id, url=url, download_dir=download_dir,
                    cookies_file=cookies_file, queue=queue,
                )

            else:
                continue

            if files:
                info.setdefault("source", source)
                queue.put(log_event(job_id, "info", f"Kaynak başarılı: {source} ({len(files)} dosya)"))
                return files, title, info

            errors.append(f"{source}: dosya döndürmedi")

        except LiveStreamError:
            raise  # canlı yayın: sıradaki kaynağa geçme

        except Exception as exc:
            message = short_error(exc)
            errors.append(f"{source}: {message}")
            queue.put(log_event(job_id, "warning", f"Kaynak başarısız [{source}]: {message}"))

            # Başarısız kaynağın yarım bıraktığı dosyalar sıradaki kaynağın
            # sonucuna karışmasın diye temizlenir.
            _clear_partial(download_dir)

    # ── Cookie teşhisi ────────────────────────────────────────────────────────
    # Tüm kaynaklar başarısız olduysa, hataların cookie kaynaklı olup
    # olmadığını sınıflandırıp ayrı kanala bildiriyoruz. Böylece admin
    # hangi platformun cookie'sini yenilemesi gerektiğini görebiliyor.
    combined = " | ".join(errors)
    cookie_reason = classify_cookie_error(combined)
    if cookie_reason:
        # Cookie'si sorunlu olan platform, linkin platformu olmayabilir:
        # Spotify indirmeleri YouTube araması üzerinden yürür. Hatanın
        # kendisi hangi platformu işaret ediyorsa o raporlanır.
        cookie_platform = error_platform_hint(combined) or platform
        queue.put(cookie_event(
            job_id=job_id,
            platform=cookie_platform,
            reason=cookie_reason,
            url=url,
            error=combined[-400:],
        ))

    raise RuntimeError("İndirme başarısız. Denemeler: " + " | ".join(errors[-5:]))


# _clear_partial artık .ytdlp içinde tanımlı (yukarıda import ediliyor);
# iki dosyada birebir aynı gövde duruyordu.
