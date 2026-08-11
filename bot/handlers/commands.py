from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.emoji_manager import em
from bot.i18n import t
from bot.live_guard import format_duration
from bot.pending import clear_user_pending
from bot.safe_message import safe_message_edit, safe_reply
from bot.storage import increment_stat
from bot.ui import (
    help_text,
    start_keyboard,
    start_text,
    unsupported_spotify_text,
    worker_started_text,
)
from bot.utils import (
    extract_first_url,
    is_spotify_url,
    is_supported_url,
    normalize_url,
)


logger = logging.getLogger("downloader")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    if update.effective_user and permissions.is_user_banned(update.effective_user.id):
        return

    config = context.application.bot_data["config"]
    is_owner = bool(update.effective_user and permissions.is_admin(update.effective_user.id))

    await safe_reply(
        update.message,
        start_text(config.bot_name),
        parse_mode="HTML",
        reply_markup=start_keyboard(is_owner),
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    check = permissions.check_update(update)

    if not check.allowed:
        if update.effective_chat and update.effective_chat.type == "private":
            await safe_reply(update.message, check.reason)
        return

    await safe_reply(
        update.message,
        help_text(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    if permissions.is_user_banned(update.effective_user.id):
        return

    manager = context.application.bot_data["process_manager"]
    config = context.application.bot_data.get("config")
    user_id = update.effective_user.id

    job = manager.get_user_active_job(user_id)
    status_msg_id = job.status_message_id if job else None
    chat_id = job.chat_id if job else None

    ok = manager.cancel_user_job(user_id)

    # Bekleyen format menüsü de iptal kapsamındadır. Önceden /cancel bunu
    # görmüyordu: kullanıcı menüyü açıp indirmeyi başlatmadan /cancel yazınca
    # "iptal edilecek işlem yok" yanıtı alıyor, menü de ekranda kalıyordu.
    pending_cancelled = bool(
        await clear_user_pending(context.application, user_id)
    )

    # Aktif bir playlist oturumu varsa onu da iptal et (item döngüsü durur).
    playlist_cancelled = False
    for pjob in context.application.bot_data.get("playlist_sessions", {}).values():
        if pjob.get("user_id") == user_id and not pjob.get("cancelled"):
            pjob["cancelled"] = True
            playlist_cancelled = True

    if ok or playlist_cancelled or pending_cancelled:
        if config:
            increment_stat(config.data_dir, "cancelled_downloads")

        if status_msg_id and chat_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
            except Exception:
                pass

        await safe_reply(update.message, "🛑 " + t("cancel_done"))
    else:
        await safe_reply(update.message, t("cancel_none"))


async def ses_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    check = permissions.check_update(update)

    if not check.allowed:
        if update.effective_chat.type == "private":
            await safe_reply(update.message, check.reason)
        return

    raw_parts: list[str] = []

    if context.args:
        raw_parts.append(" ".join(context.args))

    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        raw_parts.append(replied.text or replied.caption or "")

    raw = "\n".join(part for part in raw_parts if part)
    url = normalize_url(extract_first_url(raw) or "")

    if not url or not is_supported_url(url):
        await safe_reply(
            update.message,
            t("ses_usage"),
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
        )
        return

    # Spotify: yalnızca tekil şarkı (track) desteklenir; worker şarkıyı
    # YouTube'da bulup ses olarak indirir. Albüm/playlist desteklenmez.
    if is_spotify_url(url) and "/track/" not in url.lower():
        await safe_reply(
            update.message,
            unsupported_spotify_text(),
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id,
            disable_web_page_preview=True,
        )
        return

    bot_data = context.application.bot_data
    manager = bot_data["process_manager"]
    state = bot_data["bot_state"]
    user = update.effective_user
    chat = update.effective_chat
    is_admin = permissions.is_admin(user.id)

    # ── Canlı yayın geçici banı ───────────────────────────────────────────────
    # BYPASS: bu kontrol yalnızca link handler'ında vardı; canlı yayın spamı
    # yüzünden banlanan kullanıcı /ses ile indirmeye devam edebiliyordu.
    live_guard = bot_data.get("live_guard")
    if live_guard and not is_admin:
        import asyncio as _asyncio
        remaining = await _asyncio.to_thread(live_guard.ban_remaining, user.id)
        if remaining > 0:
            if chat.type == "private":
                await safe_reply(
                    update.message,
                    t("live_ban_active", duration=format_duration(remaining)),
                    parse_mode="HTML",
                    reply_to_message_id=update.message.message_id,
                )
            return

    # Kullanıcı/sohbet kaydı (duyuru listesi + analitik) — link akışıyla aynı.
    db = bot_data.get("db")
    if db:
        import asyncio as _asyncio
        try:
            await _asyncio.to_thread(
                db.touch_user, user.id,
                username=user.username, first_name=user.first_name,
                language=user.language_code,
            )
            await _asyncio.to_thread(
                db.touch_chat, chat.id,
                title=getattr(chat, "title", None) or "", chat_type=chat.type,
            )
        except Exception:
            logger.exception("DB kullanıcı kaydı başarısız (/ses)")

    # Bakım modu: indirme yapılmaz, sabit mesaj döner (admin hariç).
    if state.is_maintenance() and not is_admin:
        await safe_reply(update.message, t("maintenance"), reply_to_message_id=update.message.message_id)
        return

    # Safe mode: sessiz çalışma (mesaj/yazıyor yok).
    silent = state.is_safe()
    # Ses her zaman audio_best modunda — cache lookup/store ile hizalı.
    audio_mode = "audio_best"

    # Cache: aynı şarkı daha önce indirildiyse yeniden indirme.
    from bot.handlers.links import _try_serve_from_cache  # döngüsel import önlemi
    if not manager.get_user_active_job(user.id):
        served = await _try_serve_from_cache(
            context, url=url, mode=audio_mode, chat=chat, message=update.message, silent=silent,
        )
        if served:
            return

    if manager.get_user_active_job(user.id):
        if not silent:
            await safe_reply(
                update.message,
                t("wait_active"),
                reply_to_message_id=update.message.message_id,
            )
        return

    # Safe mode: durum mesajı göstermeden sessizce indir.
    if silent:
        try:
            manager.start_download(
                user_id=user.id, chat_id=chat.id,
                thread_id=update.message.message_thread_id,
                reply_to_message_id=update.message.message_id,
                url=url, mode=audio_mode, silent=True,
                username=user.username,
                chat_title=getattr(chat, "title", None), chat_type=chat.type,
            )
        except Exception:
            # Sessiz mod kullanıcıya mesaj göndermez ama hata yutulmamalı;
            # aksi halde indirme hiç başlamadığında iz kalmıyordu.
            logger.exception("Safe mode /ses indirmesi başlatılamadı | url=%s", url)
        return

    wait_msg = await safe_reply(
        update.message,
        f"{em('icon_ytmusic')} " + t("audio_preparing"),
        reply_to_message_id=update.message.message_id,
    )

    try:
        job = manager.start_download(
            user_id=user.id,
            chat_id=chat.id,
            thread_id=update.message.message_thread_id,
            reply_to_message_id=update.message.message_id,
            url=url,
            mode=audio_mode,
            username=user.username,
            chat_title=getattr(chat, "title", None),
            chat_type=chat.type,
        )
        manager.attach_status_message(job.job_id, wait_msg.message_id)

        await safe_message_edit(
            wait_msg,
            worker_started_text(url),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as exc:
        await safe_message_edit(wait_msg, t("job_start_failed"))


async def duyuru_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /duyurular — kullanıcı duyuru (broadcast) tercihini değiştirir.

    Faz 3 duyuru sistemi bu tercihi okur: opt-out yapan kullanıcılara
    duyuru gönderilmez (bkz. Database.broadcast_targets).
    """
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    db = context.application.bot_data.get("db")
    if not db:
        return

    import asyncio

    record = await asyncio.to_thread(db.get_user, user.id)
    if not record:
        await asyncio.to_thread(
            db.touch_user, user.id,
            username=user.username, first_name=user.first_name,
            language=user.language_code,
        )
        record = await asyncio.to_thread(db.get_user, user.id)

    currently_out = bool((record or {}).get("broadcast_opt_out"))
    new_state = not currently_out

    await asyncio.to_thread(
        db.set_broadcast_opt_out, user_id=user.id, opt_out=new_state
    )

    await safe_reply(
        update.message,
        t("broadcast_opt_out") if new_state else t("broadcast_opt_in"),
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id,
    )
