from __future__ import annotations

"""
bot/pending.py — YouTube format seçim menüsünün yaşam döngüsü.

Sorun:
    Kullanıcı format seçtiğinde menü mesajı ekranda kalıyordu. Kod yalnızca
    butonları kaldırıyordu (edit_message_reply_markup(None)); mesajın kendisi
    (çoğu zaman kapak fotoğrafı + uzun açıklama) sohbette duruyordu. Ayrıca:
      • yeni link gönderildiğinde eski menü mesajı siliniyordu → hayır, sadece
        butonları kaldırılıyordu, mesaj kalıyordu,
      • admin "aktif işleri temizle" dediğinde menüler tamamen ÖKSÜZ kalıyordu
        (butonlar hâlâ tıklanabilir ama iş kaydı yok),
      • menülerin süresi hiç dolmuyordu; bellekte süresiz birikiyorlardı.

Çözüm:
    Menü mesajının silinmesi tek bir yerden yapılır (clear_pending_job) ve
    TÜM yollar bunu kullanır: seçim, yeni link, iptal, hata, admin sıfırlama
    ve süre aşımı.
"""

import logging
import time
from typing import Any

logger = logging.getLogger("downloader")

# Menü bu süre boyunca dokunulmazsa temizlenir (mesaj silinir).
PENDING_TTL_SECONDS = 30 * 60


async def delete_menu_message(app: Any, job: dict[str, Any]) -> None:
    """
    Bir bekleyen işin menü mesajını siler.

    Silme başarısız olursa (mesaj zaten silinmiş, çok eski, yetki yok)
    en azından butonları kaldırmayı dener — kullanıcı tıklayıp
    "menü zaman aşımına uğradı" uyarısı almasın.
    """
    chat_id = job.get("chat_id")
    message_id = job.get("status_message_id")

    if not chat_id or not message_id:
        return

    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return
    except Exception:
        pass

    try:
        await app.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        pass


async def clear_pending_job(app: Any, job_id: str, *, delete_message: bool = True) -> dict | None:
    """Bekleyen işi kaydından düşürür ve menü mesajını temizler."""
    jobs = app.bot_data.get("pending_jobs") or {}
    job = jobs.pop(job_id, None)

    if job and delete_message:
        await delete_menu_message(app, job)

    return job


async def clear_user_pending(app: Any, user_id: int) -> dict | None:
    """Kullanıcının bekleyen menüsünü temizler (yeni link geldiğinde)."""
    jobs = app.bot_data.get("pending_jobs") or {}

    target_id = None
    for job_id, job in jobs.items():
        if job.get("user_id") == user_id:
            target_id = job_id
            break

    if not target_id:
        return None

    return await clear_pending_job(app, target_id)


async def clear_all_pending(app: Any) -> int:
    """Tüm bekleyen menüleri temizler (admin sıfırlaması)."""
    jobs = app.bot_data.get("pending_jobs") or {}
    job_ids = list(jobs.keys())

    for job_id in job_ids:
        try:
            await clear_pending_job(app, job_id)
        except Exception:
            jobs.pop(job_id, None)

    return len(job_ids)


async def expire_pending_jobs(app: Any, *, ttl: float = PENDING_TTL_SECONDS) -> int:
    """
    Süresi dolmuş menüleri temizler.

    Menüler süresiz durduğunda hem bellekte birikiyor hem de kullanıcı eski
    bir menüye tıklayınca anlamsız bir hata alıyordu. Bunun yerine mesaj
    sessizce siliniyor.
    """
    jobs = app.bot_data.get("pending_jobs") or {}
    if not jobs:
        return 0

    now = time.time()
    expired = [
        job_id for job_id, job in jobs.items()
        if now - float(job.get("created_at") or now) > ttl
    ]

    for job_id in expired:
        try:
            await clear_pending_job(app, job_id)
        except Exception:
            jobs.pop(job_id, None)

    if expired:
        logger.info("Süresi dolan %d format menüsü temizlendi.", len(expired))

    return len(expired)
