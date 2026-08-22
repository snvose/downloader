from __future__ import annotations

"""
Multi-source download pipeline.

Tries a link against the platform-specific source order:
    cobalt → yt-dlp → gallery-dl   (order configurable via data/sources.json)

When a source fails the next one is tried; if all fail, a combined error is
raised. The livestream check runs once, before any source is tried (see
bot/live_guard.py).
"""

import time
from pathlib import Path
from typing import Any

from bot.cookie_health import classify_cookie_error, error_platform_hint
from bot.cookie_policy import CookieCooldown, needs_cookies
from bot.live_guard import can_be_live, probe_is_live
from bot.queue_events import cookie_event, log_event, progress_event
from bot.utils import platform_name

from .cobalt import CobaltClient, CobaltError, CobaltUnavailable, platform_supported
from .sources import SourcePriority
from .ytdlp import (
    LiveStreamError,
    _clear_partial_files as _clear_partial,
    _download_with_gallery_dl,
    _is_spotify_url,
    download_with_ytdlp,
    short_error,
)


def _cobalt_progress(job_id: str, queue: Any):
    """Forwards cobalt download progress to the bot's queue (once a second)."""
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
        raise CobaltUnavailable("cobalt is not configured.")

    if not platform_supported(platform):
        raise CobaltUnavailable(f"cobalt does not support this platform: {platform}")

    files, cinfo = client.download(
        url=url,
        download_dir=download_dir,
        mode=mode,
        subtitle_lang=subtitle_lang,
        on_progress=_cobalt_progress(job_id, queue),
    )

    if not files:
        raise CobaltError("cobalt returned no files.")

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
    Tries the link against the platform's ordered sources and returns the
    first success.

    LiveStreamError propagates up unchanged — a livestream is never tried
    against any source.
    """
    platform = platform_name(url)
    download_dir = Path(download_dir)

    # ── Livestream check (once, before any source) ───────────────────────────
    # Deliberately cookieless: live status is public, and the authenticated
    # query measured about twice as slow. Link shapes that cannot be live
    # (a reel, a tweet, a pin) skip the request entirely — see can_be_live().
    if not _is_spotify_url(url) and can_be_live(url):
        is_live, _probe = probe_is_live(url)
        if is_live:
            queue.put(log_event(job_id, "warning", f"Livestream rejected: {url}"))
            raise LiveStreamError("Livestreams cannot be downloaded.")

    # ── Available sources ──────────────────────────────────────────────────
    available = {"ytdlp", "gallerydl"}
    if cobalt and cobalt.enabled and platform_supported(platform):
        available.add("cobalt")

    # Spotify only has a yt-dlp path (metadata -> YouTube search).
    if _is_spotify_url(url):
        order = ["ytdlp"]
    else:
        priority = priority or SourcePriority(download_dir.parent.parent)
        order = priority.for_platform(platform, available=available)

    queue.put(log_event(job_id, "info", f"Source order [{platform}]: {' → '.join(order)}"))

    errors: list[str] = []

    for source in order:
        try:
            queue.put(log_event(job_id, "info", f"Trying source: {source}"))

            if source == "cobalt":
                files, title, info = _run_cobalt(
                    job_id=job_id, url=url, download_dir=download_dir,
                    queue=queue, mode=mode, client=cobalt, platform=platform,
                    subtitle_lang=subtitle_lang,
                )

            elif source == "ytdlp":
                # The pipeline handles the gallery-dl fallback itself, so
                # yt-dlp must not retry the same source internally.
                files, title, info = download_with_ytdlp(
                    job_id=job_id, url=url, download_dir=download_dir,
                    queue=queue, cookies_file=cookies_file, mode=mode,
                    allow_gallery_fallback=False,
                    skip_live_check=True,  # already done above
                    subtitle_lang=subtitle_lang,
                )

            elif source == "gallerydl":
                files, title, info = _download_with_gallery_dl(
                    job_id=job_id, url=url, download_dir=download_dir,
                    cookies_file=cookies_file, queue=queue,
                    use_cookies=needs_cookies(url) or not CookieCooldown(
                        download_dir.parent.parent
                    ).active(platform),
                )

            else:
                continue

            if files:
                info.setdefault("source", source)
                queue.put(log_event(job_id, "info", f"Source succeeded: {source} ({len(files)} files)"))
                return files, title, info

            errors.append(f"{source}: returned no files")

        except LiveStreamError:
            raise  # livestream: don't try the next source

        except Exception as exc:
            message = short_error(exc)
            errors.append(f"{source}: {message}")
            queue.put(log_event(job_id, "warning", f"Source failed [{source}]: {message}"))

            # Clean up whatever the failed source left behind so it doesn't
            # bleed into the next source's result.
            _clear_partial(download_dir)

    # ── Cookie diagnosis ──────────────────────────────────────────────────────
    # If every source failed, classify whether it was cookie related and log
    # it separately so the admin can see which platform's cookie to refresh.
    combined = " | ".join(errors)
    cookie_reason = classify_cookie_error(combined)
    if cookie_reason:
        # The platform with the bad cookie may differ from the link's own
        # platform (Spotify runs through a YouTube search), so the error
        # message itself decides which platform gets reported.
        cookie_platform = error_platform_hint(combined) or platform
        queue.put(cookie_event(
            job_id=job_id,
            platform=cookie_platform,
            reason=cookie_reason,
            url=url,
            error=combined[-400:],
        ))

    raise RuntimeError("Download failed. Attempts: " + " | ".join(errors[-5:]))
