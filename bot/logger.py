from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

from .config import Config
from .download_log import setup_download_logger
from .log_buffer import install_ring_buffer


def setup_logging(config: Config) -> logging.Logger:
    log_file = config.log_dir / "bot.log"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(logging.INFO)
        root.addHandler(console)

    # boyut tabanlı yerine GÜNLÜK rotasyon + retention süresi (7 gün varsayılan)
    file_exists = any(
        isinstance(handler, TimedRotatingFileHandler)
        and getattr(handler, "baseFilename", "") == str(log_file)
        for handler in root.handlers
    )

    if not file_exists:
        file_handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=max(1, int(config.log_retention_days)),
            encoding="utf-8",
            utc=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    # indirme olayları için ayrı günlük log (data/logs/downloads.log-*)
    setup_download_logger(config.log_dir, config.log_retention_days)

    # admin bildiriminde son log satırlarını sunmak için ring buffer
    install_ring_buffer()

    return logging.getLogger("downloader")
