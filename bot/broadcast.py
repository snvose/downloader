from __future__ import annotations

"""
bot/broadcast.py — toplu duyuru gönderimi.

Tasarım kararları:

  • HIZ SINIRI: Telegram farklı sohbetlere saniyede ~30 mesaja izin verir.
    Güvenli tarafta kalmak için varsayılan 20/sn (0.05 sn aralık). Telegram
    yine de RetryAfter dönerse istenen süre beklenir ve o hedef tekrar denenir.

  • KALICI OLARAK ERİŞİLEMEYENLER: botu engelleyen / hesabı silinen / bottan
    atılan hedefler DB'de is_blocked=1 ile işaretlenir. Bir sonraki duyuruda
    bu kayıtlar hedef listesine hiç girmez — her seferinde onlarca boşa istek
    atılmaz.

  • GEÇİCİ HATA vs KALICI HATA ayrımı önemlidir: "bot was blocked" kalıcıdır
    (işaretle), "timeout / bad gateway" geçicidir (işaretleme, sadece say).

  • İPTAL: uzun süren gönderim admin tarafından durdurulabilir.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("downloader")

# Saniyedeki mesaj sayısı (Telegram üst sınırı ~30; pay bırakıyoruz)
MESSAGES_PER_SECOND = 20
SEND_DELAY = 1.0 / MESSAGES_PER_SECOND

# Bu ifadeler hedefin KALICI olarak erişilemez olduğunu gösterir.
_PERMANENT_MARKERS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "peer_id_invalid",
    "bot was kicked",
    "the group chat was deleted",
    "chat_write_forbidden",
    "not enough rights to send text messages",
    "have no rights to send a message",
    "user_is_blocked",
)


def _is_permanent_failure(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _PERMANENT_MARKERS)


@dataclass
class BroadcastJob:
    """Tek bir duyuru gönderiminin durumu."""

    text: str
    targets: list[int]
    kind: str = "all"                 # all | users | groups
    parse_mode: str | None = "HTML"
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    sent: int = 0
    failed: int = 0
    blocked: int = 0                  # kalıcı erişilemez (DB'de işaretlendi)
    cancelled: bool = False
    running: bool = False

    # Örnek hata mesajları (admin raporunda gösterilir)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.targets)

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.blocked

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def progress_text(self) -> str:
        done = self.processed
        total = self.total or 1
        percent = int(done * 100 / total)
        width = 12
        filled = int(done / total * width)
        bar = "█" * filled + "░" * (width - filled)

        state = "🛑 İptal edildi" if self.cancelled else (
            "📤 Gönderiliyor" if self.running else "✅ Tamamlandı"
        )

        return (
            f"<b>{state}</b>\n\n"
            f"<code>[{bar}]</code> <b>{percent}%</b>\n"
            f"İşlenen: <b>{done}</b> / {self.total}\n"
            f"✅ Ulaşan: <b>{self.sent}</b>\n"
            f"🚫 Engellemiş: <b>{self.blocked}</b>\n"
            f"⚠️ Hata: <b>{self.failed}</b>\n"
            f"⏱ Süre: <b>{self.duration:.0f} sn</b>"
        )

    def summary_text(self) -> str:
        """Gönderim sonrası özet rapor."""
        lines = [
            "🛑 <b>Duyuru iptal edildi</b>" if self.cancelled
            else "✅ <b>Duyuru tamamlandı</b>",
            "",
            f"📊 Hedef: <b>{self.total}</b>",
            f"✅ Ulaşan: <b>{self.sent}</b>",
            f"🚫 Engellemiş / erişilemez: <b>{self.blocked}</b>",
            f"⚠️ Geçici hata: <b>{self.failed}</b>",
            f"⏱ Süre: <b>{self.duration:.0f} saniye</b>",
        ]

        if self.total:
            rate = self.sent * 100 / self.total
            lines.append(f"📈 Başarı oranı: <b>%{rate:.0f}</b>")

        if self.blocked:
            lines.append(
                f"\n<i>{self.blocked} kayıt işaretlendi; bir sonraki duyuruda "
                "bunlara tekrar denenmeyecek.</i>"
            )

        if self.errors:
            sample = "\n".join(f"• {e}" for e in self.errors[:3])
            lines.append(f"\n<b>Örnek hatalar</b>\n{sample}")

        return "\n".join(lines)


async def run_broadcast(
    app: Any,
    job: BroadcastJob,
    *,
    db: Any = None,
    on_progress=None,
    progress_every: int = 25,
) -> BroadcastJob:
    """
    Duyuruyu kuyruklu ve hız sınırlı biçimde gönderir.

    on_progress: her `progress_every` hedefte bir çağrılan async callback.
    """
    job.running = True
    job.started_at = time.time()

    for index, chat_id in enumerate(job.targets, start=1):
        if job.cancelled:
            break

        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=job.text,
                parse_mode=job.parse_mode,
                disable_web_page_preview=True,
            )
            job.sent += 1

        except Exception as exc:
            # Telegram "çok hızlısın" derse istediği kadar bekle ve tekrar dene.
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                logger.warning("Duyuru flood limiti: %s sn bekleniyor", retry_after)
                await asyncio.sleep(float(retry_after) + 1.0)
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=job.text,
                        parse_mode=job.parse_mode,
                        disable_web_page_preview=True,
                    )
                    job.sent += 1
                except Exception as retry_exc:
                    exc = retry_exc
                else:
                    await asyncio.sleep(SEND_DELAY)
                    continue

            if _is_permanent_failure(exc):
                job.blocked += 1
                if db:
                    try:
                        # Özel sohbette chat_id == user_id
                        await asyncio.to_thread(
                            db.mark_blocked,
                            user_id=chat_id if chat_id > 0 else None,
                            chat_id=chat_id if chat_id < 0 else None,
                        )
                    except Exception:
                        logger.exception("Engellenen hedef işaretlenemedi: %s", chat_id)
            else:
                job.failed += 1
                if len(job.errors) < 5:
                    job.errors.append(f"{chat_id}: {str(exc)[:90]}")

        # Hız sınırı
        await asyncio.sleep(SEND_DELAY)

        if on_progress and index % progress_every == 0:
            try:
                await on_progress(job)
            except Exception:
                logger.exception("Duyuru ilerleme bildirimi başarısız")

    job.running = False
    job.finished_at = time.time()

    logger.info(
        "DUYURU tamamlandı | hedef=%d ulaşan=%d engellemiş=%d hata=%d süre=%.0fsn",
        job.total, job.sent, job.blocked, job.failed, job.duration,
    )

    if on_progress:
        try:
            await on_progress(job)
        except Exception:
            pass

    return job
