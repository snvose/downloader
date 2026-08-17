from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = DATA_DIR / ".env"


@dataclass(frozen=True)
class Config:
    base_dir: Path
    data_dir: Path
    bot_name: str
    bot_username: str
    bot_token: str
    admin_id: int
    local_bot_api_base: str
    max_simultaneous_downloads: int
    max_file_size_mb: int
    cookies_file: Path
    download_dir: Path
    log_dir: Path
    cleanup_tz_offset: int        # UTC offset used for the daily cleanup clock
    cleanup_hour: int             # hour of day the cleanup runs
    log_retention_days: int       # how long daily log files are kept
    cache_enabled: bool           # reuse file_id for links sent again
    show_links: bool              # show owner/community buttons in /start
    owner_link: str
    community_link: str
    community_label: str
    # Resource protection
    job_timeout_sec: int          # hard time limit for a single download
    job_max_bytes: int            # hard disk limit for a single download
    live_strike_limit: int        # livestream attempts before a temporary ban
    live_ban_days: int
    # cobalt (self-hosted instance; source is disabled when empty)
    cobalt_api_url: str
    cobalt_api_key: str
    cobalt_timeout: int
    # Database
    db_path: Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be a number: {value}")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _path_from_env(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip() or default
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def load_config() -> Config:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(ENV_FILE)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token or bot_token.startswith("YOUR_"):
        raise RuntimeError("BOT_TOKEN is missing in data/.env")

    admin_raw = os.getenv("ADMIN_ID", "").strip()
    if not admin_raw or admin_raw.startswith("YOUR_"):
        raise RuntimeError("ADMIN_ID is missing in data/.env")

    try:
        admin_id = int(admin_raw)
    except ValueError:
        raise RuntimeError("ADMIN_ID must be a numeric Telegram user id.")

    local_base = os.getenv("LOCAL_BOT_API_BASE", "").strip().rstrip("/")
    if local_base.endswith("/bot"):
        local_base = local_base[:-4].rstrip("/")

    download_dir = _path_from_env("DOWNLOAD_DIR", "data/downloads")
    log_dir = _path_from_env("LOG_DIR", "data/logs")
    cookies_file = _path_from_env("COOKIES_FILE", "data/cookies.txt")

    download_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        base_dir=BASE_DIR,
        data_dir=DATA_DIR,
        bot_name=os.getenv("BOT_NAME", "Downloader").strip() or "Downloader",
        bot_username=os.getenv("BOT_USERNAME", "").strip(),
        bot_token=bot_token,
        admin_id=admin_id,
        local_bot_api_base=local_base,
        max_simultaneous_downloads=_env_int("MAX_SIMULTANEOUS_DOWNLOADS", 3),
        max_file_size_mb=_env_int("MAX_FILE_SIZE_MB", 1900),
        cookies_file=cookies_file,
        download_dir=download_dir,
        log_dir=log_dir,
        cleanup_tz_offset=_env_int("CLEANUP_TZ_OFFSET", 0),
        cleanup_hour=_env_int("CLEANUP_HOUR", 0),
        log_retention_days=_env_int("LOG_RETENTION_DAYS", 7),
        cache_enabled=_env_bool("CACHE_ENABLED", True),
        show_links=_env_bool("SHOW_LINKS", False),
        owner_link=os.getenv("OWNER_LINK", "").strip(),
        community_link=os.getenv("COMMUNITY_LINK", "").strip(),
        community_label=os.getenv("COMMUNITY_LABEL", "Community").strip() or "Community",
        # 30 min: even the longest legitimate download stays far below this,
        # while an endless stream hits it.
        job_timeout_sec=_env_int("JOB_TIMEOUT_SEC", 1800),
        job_max_bytes=_env_int("JOB_MAX_GB", 4) * 1024 * 1024 * 1024,
        live_strike_limit=_env_int("LIVE_STRIKE_LIMIT", 3),
        live_ban_days=_env_int("LIVE_BAN_DAYS", 5),
        # cobalt has no public shared API; you must run your own instance.
        # Left empty, the cobalt source is skipped and yt-dlp is used.
        cobalt_api_url=os.getenv("COBALT_API_URL", "").strip().rstrip("/"),
        cobalt_api_key=os.getenv("COBALT_API_KEY", "").strip(),
        cobalt_timeout=_env_int("COBALT_TIMEOUT", 30),
        db_path=_path_from_env("DB_PATH", "data/bot.db"),
    )
