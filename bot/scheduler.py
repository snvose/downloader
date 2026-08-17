from __future__ import annotations

"""
Daily cleanup of the downloads directory.

Runs on plain asyncio (no external cron, no APScheduler). Directories that
belong to a running job are skipped so an in-flight download is never wiped.
"""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .cache import MediaCache
from .config import Config

logger = logging.getLogger("downloader")


def _seconds_until_next_run(tz_offset_hours: int, run_hour: int) -> float:
    """Seconds until the next run_hour:00 in the configured offset."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz)
    target = now.replace(hour=run_hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _cleanup_downloads(download_dir: Path, keep: Iterable[Path] = ()) -> tuple[int, int]:
    """
    Removes everything inside download_dir except the paths in `keep`.
    Returns (removed_files, freed_bytes).
    """
    removed_files = 0
    freed_bytes = 0

    if not download_dir.exists():
        return 0, 0

    keep_set = {Path(p).resolve() for p in keep}

    for child in download_dir.iterdir():
        try:
            if child.resolve() in keep_set:
                continue
        except OSError:
            continue

        # Count first so the log reflects what was actually freed.
        if child.is_dir():
            for root, _, names in os.walk(child):
                for name in names:
                    try:
                        freed_bytes += (Path(root) / name).stat().st_size
                        removed_files += 1
                    except OSError:
                        pass
        else:
            try:
                freed_bytes += child.stat().st_size
                removed_files += 1
            except OSError:
                pass

        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Cleanup: could not remove %s: %s", child, exc)

    return removed_files, freed_bytes


def run_cleanup_once(
    config: Config,
    cache: MediaCache,
    keep: Iterable[Path] = (),
) -> dict:
    """Synchronous cleanup (call through to_thread). Returns a summary."""
    removed_files, freed_bytes = _cleanup_downloads(config.download_dir, keep)
    pruned_records = cache.prune_missing_files()
    return {
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "pruned_cache_records": pruned_records,
    }


async def cleanup_scheduler(
    config: Config,
    cache: MediaCache,
    active_dirs: Callable[[], Iterable[Path]] | None = None,
) -> None:
    """
    Runs the cleanup every day at the configured hour.

    active_dirs: callable returning the download directories of running jobs,
    which are left untouched.
    """
    from .utils import human_bytes  # late import

    logger.info(
        "Cleanup scheduler active: every day at %02d:00 (UTC%+d).",
        config.cleanup_hour, config.cleanup_tz_offset,
    )

    while True:
        try:
            wait_s = _seconds_until_next_run(config.cleanup_tz_offset, config.cleanup_hour)
            await asyncio.sleep(wait_s)

            keep = list(active_dirs()) if active_dirs else []
            result = await asyncio.to_thread(run_cleanup_once, config, cache, keep)

            logger.info(
                "Daily cleanup done: %d files removed, %s freed, "
                "%d cache records pruned.",
                result["removed_files"],
                human_bytes(result["freed_bytes"]),
                result["pruned_cache_records"],
            )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Cleanup scheduler error")
            await asyncio.sleep(60)
