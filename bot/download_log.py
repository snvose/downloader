from __future__ import annotations

"""
Structured logging of download events.

Every download is logged with: timestamp, user id + username, chat id, chat
title, chat type, platform, URL, result, file size and duration.

Logs live in data/logs/ as daily files and are removed automatically after
LOG_RETENTION_DAYS days. Failures are additionally written with a full
traceback.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .utils import human_bytes

_download_logger = logging.getLogger("downloads")


def setup_download_logger(log_dir: Path, retention_days: int = 7) -> logging.Logger:
    """data/logs/downloads.log — rotates nightly, kept for retention_days."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "downloads.log"

    already = any(
        isinstance(h, TimedRotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(log_file)
        for h in _download_logger.handlers
    )
    if not already:
        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=max(1, int(retention_days)),
            encoding="utf-8",
            utc=False,
        )
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        _download_logger.addHandler(handler)

    _download_logger.setLevel(logging.INFO)
    _download_logger.propagate = False  # keep it out of the main log
    return _download_logger


def log_download(
    *,
    user_id: int | None,
    username: str | None,
    chat_id: int | None,
    chat_title: str | None,
    chat_type: str | None,
    platform: str | None,
    url: str,
    result: str,
    file_size: int | None = None,
    duration: float | None = None,
    extra: str = "",
) -> None:
    """Logs a single download as one structured line."""
    size_text = human_bytes(file_size) if file_size else "-"
    dur_text = f"{duration:.1f}s" if duration is not None else "-"
    parts = [
        f"result={result}",
        f"platform={platform or '-'}",
        f"user={user_id}",
        f"username=@{username}" if username else "username=-",
        f"chat={chat_id}",
        f"chat_title={chat_title or '-'}",
        f"chat_type={chat_type or '-'}",
        f"size={size_text}",
        f"duration={dur_text}",
        f"url={url}",
    ]
    if extra:
        parts.append(f"note={extra}")
    _download_logger.info(" | ".join(parts))


def log_download_error(*, url: str, platform: str | None, traceback_text: str) -> None:
    _download_logger.error(
        "result=error | platform=%s | url=%s\nTRACEBACK:\n%s",
        platform or "-", url, traceback_text,
    )
