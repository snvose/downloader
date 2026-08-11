from __future__ import annotations

"""
bot/admin_notify.py — Admine hata bildirimi.

Bot bir medya indirmeyi tamamlayamazsa admine:
  - hata özeti
  - son 20 satır log
  - iki buton: "Safe Mode'a Geç" ve "Bakım Modunu Aç"
gönderilir.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .log_buffer import last_lines

logger = logging.getLogger("downloader")


def _notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔇 Safe Mode'a Geç", callback_data="admin|mode|safe"),
            InlineKeyboardButton("🛠 Bakım Modu", callback_data="admin|mode|maintenance"),
        ],
        [InlineKeyboardButton("⚙️ Admin Paneli", callback_data="admin|panel")],
    ])


async def notify_admin_failure(
    bot,
    admin_id: int,
    *,
    summary: str,
    url: str = "",
    platform: str = "",
    user_id: int | None = None,
    username: str | None = None,
    chat_id: int | None = None,
    chat_title: str | None = None,
) -> None:
    """Admine hata özeti + kullanıcı/sohbet bilgisi + son 20 log satırı + iki buton gönderir."""
    if not admin_id:
        return

    tail = "\n".join(last_lines(20))
    if len(tail) > 3000:
        tail = tail[-3000:]

    if username:
        user_line = f"@{html.escape(username)} (id: {user_id or '-'})"
    else:
        user_line = f"id: {user_id or '-'}"

    if chat_title:
        chat_line = f"{html.escape(str(chat_title))} (id: {chat_id if chat_id is not None else '-'})"
    else:
        chat_line = f"id: {chat_id if chat_id is not None else '-'}"

    text = (
        "<b>İndirme tamamlanamadı</b>\n\n"
        f"<b>Kullanıcı:</b> {user_line}\n"
        f"<b>Sohbet:</b> {chat_line}\n"
        f"<b>Özet:</b> {html.escape(summary[:400])}\n"
        f"<b>Platform:</b> {html.escape(platform or '-')}\n"
        f"<b>URL:</b> {html.escape(url[:300] or '-')}\n\n"
        f"<b>Son 20 satır log:</b>\n<pre>{html.escape(tail)}</pre>"
    )

    try:
        await bot.send_message(
            chat_id=admin_id,
            text=text[:4096],
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_notify_keyboard(),
        )
    except Exception:
        # Bildirim başarısız olursa botu düşürme; sadece logla.
        logger.exception("Admin hata bildirimi gönderilemedi.")
