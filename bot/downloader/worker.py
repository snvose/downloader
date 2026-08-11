from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any

from bot.downloader.cobalt import CobaltClient
from bot.downloader.pipeline import download_media
from bot.downloader.sources import SourcePriority
from bot.downloader.ytdlp import LiveStreamError
from bot.queue_events import done_event, error_event, log_event


def worker_entry(job: dict[str, Any], queue: Any) -> None:
    job_id = str(job["job_id"])
    url = str(job["url"])
    download_dir = Path(job["download_dir"])
    cookies_file = Path(job["cookies_file"]) if job.get("cookies_file") else None
    mode = str(job.get("mode") or "auto")
    data_dir = Path(job["data_dir"]) if job.get("data_dir") else download_dir.parent.parent
    subtitle_lang = str(job.get("subtitle_lang") or "")

    # ── Süreç grubu izolasyonu ────────────────────────────────────────────────
    # Kendi oturumumuzu (session + process group) açıyoruz. yt-dlp bazı akışları
    # bir ffmpeg alt sürecine devreder; grup lideri olmadan worker'ı öldürmek
    # ffmpeg'i ÖKSÜZ bırakıyordu — ffmpeg diske yazmaya devam ediyor, bot onu
    # göremiyor ve durduramıyordu. Grup lideri olunca ProcessManager tüm grubu
    # tek seferde öldürebiliyor.
    try:
        os.setsid()
    except (OSError, AttributeError):
        pass  # Windows / zaten grup lideri

    try:
        download_dir.mkdir(parents=True, exist_ok=True)

        queue.put(log_event(job_id, "info", f"Worker başladı. mode={mode} pgid={os.getpgrp()}"))

        files, title, info = download_media(
            job_id=job_id,
            url=url,
            download_dir=download_dir,
            queue=queue,
            cookies_file=cookies_file,
            mode=mode,
            priority=SourcePriority(data_dir),
            cobalt=CobaltClient(
                api_url=str(job.get("cobalt_api_url") or ""),
                api_key=str(job.get("cobalt_api_key") or ""),
                timeout=int(job.get("cobalt_timeout") or 30),
                max_bytes=int(job.get("job_max_bytes") or 4 * 1024**3),
            ),
            subtitle_lang=subtitle_lang,
        )

        queue.put(done_event(
            job_id=job_id,
            files=files,
            title=title,
            source_url=url,
            info=info,
            mode=mode,
        ))

    except LiveStreamError as exc:
        # Kullanıcıya net mesaj gitsin diye ayrı olay tipi.
        queue.put(error_event(
            job_id=job_id,
            error=f"LiveStreamError: {exc}",
            public_message=str(exc),
            kind="live",
        ))

    except BaseException as exc:
        queue.put(error_event(
            job_id=job_id,
            error=traceback.format_exc(),
            public_message=str(exc),
        ))
