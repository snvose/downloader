from __future__ import annotations

"""
bot/handlers/gate.py — banlı kullanıcı/grup için global giriş kapısı.

Ban kontrolü önceden her handler'ın kendi içindeydi ve bu yüzden eksikti:
buton (callback) handler'ında hiç yoktu, /start ve /cancel yalnızca
kullanıcı banına bakıyordu, grup banına bakmıyordu. Yani banlı bir grupta
menü butonları ve komutların bir kısmı çalışmaya devam ediyordu.

Bu kapı en düşük handler grubunda (-2) çalışır ve banlıysa
ApplicationHandlerStop ile diğer TÜM handler'ları durdurur. Böylece yeni
bir handler eklendiğinde ban kontrolünü unutmak mümkün değil.

Bakım modu KASITLI olarak burada değil: onun mesajı komuta göre değişiyor
(bkz. Permissions.check_update) ve /cancel gibi komutların bakım modunda da
çalışması gerekiyor.
"""

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger("downloader")


async def ban_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    permissions = context.application.bot_data.get("permissions")
    if permissions is None:
        return

    user = update.effective_user
    chat = update.effective_chat

    user_id = user.id if user else None
    chat_id = chat.id if chat else None

    # Admin hiçbir zaman kapıda durdurulmaz (kendini kilitleme riski).
    if permissions.is_admin(user_id):
        return

    if permissions.is_user_banned(user_id):
        reason = "Bu kullanıcı botu kullanamaz."
    elif permissions.is_group_banned(chat_id):
        reason = "Bot bu grupta kullanılamıyor."
    else:
        return

    # Butona basıldıysa sadece basana görünen bir uyarı ver; ölü buton
    # bırakmak yerine ne olduğunu söylemek daha iyi. Mesajlara sessiz
    # kalınır — banlı bir sohbete sürekli yanıt yazmak spam olur.
    query = update.callback_query
    if query is not None:
        try:
            await query.answer(reason, show_alert=True)
        except Exception:
            pass

    raise ApplicationHandlerStop
