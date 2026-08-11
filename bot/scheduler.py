from __future__ import annotations

"""
bot/scheduler.py — Günlük downloads/ temizliği (B-yeni özellik).

Talep:
  - downloads/ her gün GMT-3 (yapılandırılabilir) saat 00:00'da otomatik temizlensin.
  - Silinen dosya sayısı ve boşaltılan alan loglansın.
  - Cache'deki dosyası silinmiş kayıtlar da temizlensin.
  - Harici cron YOK; saf asyncio kullanılır.

asyncio tabanlı; harici bağımlılık (APScheduler) eklemeden çalışır.
"""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cache import MediaCache
from .config import Config

logger = logging.getLogger("downloader")


def _seconds_until_next_run(tz_offset_hours: int, run_hour: int) -> float:
    """Verilen saat diliminde bir sonraki run_hour:00'a kadar saniye döner."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz)
    target = now.replace(hour=run_hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _cleanup_downloads(download_dir: Path) -> tuple[int, int]:
    """
    downloads/ altındaki tüm dosyaları siler.
    (silinen_dosya_sayısı, boşaltılan_bayt) döner.
    """
    removed_files = 0
    freed_bytes = 0

    if not download_dir.exists():
        return 0, 0

    # Önce boyutları say (loglama için), sonra alt klasörleri sil.
    for root, _, names in os.walk(download_dir):
        for name in names:
            path = Path(root) / name
            try:
                freed_bytes += path.stat().st_size
                removed_files += 1
            except OSError:
                pass

    # İçindeki her şeyi sil ama download_dir kökünü koru.
    for child in download_dir.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Temizlik: %s silinemedi: %s", child, exc)

    return removed_files, freed_bytes


def run_cleanup_once(config: Config, cache: MediaCache) -> dict:
    """Senkron temizlik (to_thread ile çağrılır). Sonuç özetini döner."""
    removed_files, freed_bytes = _cleanup_downloads(config.download_dir)
    pruned_records = cache.prune_missing_files()
    return {
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "pruned_cache_records": pruned_records,
    }


async def cleanup_scheduler(config: Config, cache: MediaCache) -> None:
    """
    Sonsuz döngü: her gün hedef saatte temizliği çalıştırır.
    Blocking dosya işlemleri asyncio.to_thread() ile thread'e taşınır.
    """
    from .utils import human_bytes  # geç import

    logger.info(
        "Temizlik zamanlayıcı aktif: her gün %02d:00 (GMT%+d).",
        config.cleanup_hour, config.cleanup_tz_offset,
    )

    while True:
        try:
            wait_s = _seconds_until_next_run(config.cleanup_tz_offset, config.cleanup_hour)
            await asyncio.sleep(wait_s)

            # Blocking I/O'yu event loop dışına al
            result = await asyncio.to_thread(run_cleanup_once, config, cache)

            logger.info(
                "Günlük temizlik tamamlandı: %d dosya silindi, %s boşaltıldı, "
                "%d cache kaydı temizlendi.",
                result["removed_files"],
                human_bytes(result["freed_bytes"]),
                result["pruned_cache_records"],
            )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Temizlik zamanlayıcı hatası")
            await asyncio.sleep(60)  # hata sonrası kısa bekleme
