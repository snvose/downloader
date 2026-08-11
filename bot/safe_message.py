from __future__ import annotations

import logging
import re
from typing import Any

from telegram.error import BadRequest

logger = logging.getLogger("downloader")


TG_EMOJI_RE = re.compile(
    r'<tg-emoji emoji-id="\d+">.*?</tg-emoji>',
    re.DOTALL,
)


def strip_custom_emoji(text: str) -> str:
    return TG_EMOJI_RE.sub("✨", str(text or ""))


def is_entity_error(exc: Exception) -> bool:
    raw = str(exc)
    return (
        "Entity_text_invalid" in raw
        or "can't parse entities" in raw.lower()
        or "can't find end tag" in raw.lower()
    )


def is_topic_closed(exc: Exception) -> bool:
    """
    Mesaj kapalı bir forum topic'ine gönderilmeye çalışıldı mı?

    Telegram forum gruplarında bir topic kapatıldığında oraya mesaj
    gönderilemez; reply_text çağrısı BadRequest("Topic_closed") ile
    başarısız olur. Konu kapalı olsa bile grubun "General" başlığı
    genellikle açıktır, oraya düşebiliriz.
    """
    raw = str(exc).lower()
    return "topic_closed" in raw or "topic closed" in raw


def is_thread_missing(exc: Exception) -> bool:
    """Topic silinmiş / bulunamıyor."""
    raw = str(exc).lower()
    return (
        "message thread not found" in raw
        or "topic_deleted" in raw
        or "thread not found" in raw
    )


async def safe_reply(message: Any, text: str, **kwargs: Any):
    """
    Yanıt gönderir; Telegram'ın öngörülebilir retlerini tolere eder.

    Ele alınan durumlar:
      • premium emoji/HTML entity hatası → sade metinle tekrar dener
      • kapalı veya silinmiş forum topic'i → topic'siz (General) gönderir

    Hiçbir yol işe yaramazsa None döner (istisna fırlatmaz); çağıran
    tarafın akışı bir mesaj gönderilemedi diye çökmemeli.
    """
    try:
        return await message.reply_text(text, **kwargs)

    except BadRequest as exc:
        if is_entity_error(exc):
            kwargs["parse_mode"] = "HTML"
            try:
                return await message.reply_text(strip_custom_emoji(text), **kwargs)
            except BadRequest as retry_exc:
                exc = retry_exc

        if is_topic_closed(exc) or is_thread_missing(exc):
            # Topic kapalı: yanıt/thread bağını bırakıp gruba (General) gönder.
            fallback = dict(kwargs)
            fallback.pop("reply_to_message_id", None)
            fallback.pop("message_thread_id", None)
            fallback.pop("reply_parameters", None)
            try:
                chat = getattr(message, "chat", None)
                if chat is not None:
                    return await chat.send_message(text, **fallback)
            except Exception as fallback_exc:
                logger.warning(
                    "Kapalı topic sonrası gönderim de başarısız: %s", fallback_exc
                )
            return None

        raise


async def safe_message_edit(message: Any, text: str, **kwargs: Any):
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if not is_entity_error(exc):
            raise

        kwargs["parse_mode"] = "HTML"
        return await message.edit_text(strip_custom_emoji(text), **kwargs)


async def safe_query_edit(query: Any, text: str, **kwargs: Any):
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if not is_entity_error(exc):
            raise

        kwargs["parse_mode"] = "HTML"
        return await query.edit_message_text(strip_custom_emoji(text), **kwargs)
