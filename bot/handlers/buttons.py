from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.i18n import t
from bot.pending import clear_pending_job

logger = logging.getLogger("downloader")
from bot.handlers.emoji_admin import handle_emoji_callback
from bot.handlers.links import (
    _build_playlist_type_keyboard,
    _run_playlist_download,
    build_playlist_header,
    build_playlist_track_keyboard,
    build_youtube_action_keyboard,
    build_youtube_info_caption,
)
from bot.safe_message import safe_query_edit, safe_reply
from bot.ui import (
    back_keyboard,
    description_messages,
    details_messages,
    help_text,
    media_info_text,
    owner_keyboard,
    owner_text,
    start_keyboard,
    start_text,
)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if await handle_emoji_callback(update, context):
        return

    data = query.data or ""
    user_id = query.from_user.id if query.from_user else None

    config = context.application.bot_data["config"]
    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    is_owner = permissions.is_admin(user_id)

    if data == "menu|main":
        await query.answer()
        await safe_query_edit(
            query,
            start_text(config.bot_name),
            parse_mode="HTML",
            reply_markup=start_keyboard(is_owner),
            disable_web_page_preview=True,
        )
        return

    if data == "menu|help":
        await query.answer()
        await safe_query_edit(
            query,
            help_text(),
            parse_mode="HTML",
            reply_markup=back_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "menu|owner":
        if not is_owner:
            await query.answer("Bu alan sadece owner için.", show_alert=True)
            return

        # Eski "Owner Settings" menüsü kaldırıldı; yeni gelişmiş admin paneli açılır.
        from bot.handlers.admin import _panel_keyboard, _panel_text
        state = context.application.bot_data["bot_state"]
        await query.answer()
        await safe_query_edit(
            query,
            _panel_text(context),
            parse_mode="HTML",
            reply_markup=_panel_keyboard(state),
            disable_web_page_preview=True,
        )
        return

    if data == "owner|toggle":
        if not is_owner:
            await query.answer("Bu alan sadece owner için.", show_alert=True)
            return

        enabled = not permissions.get_bot_enabled()
        permissions.set_bot_enabled(enabled)

        if not enabled:
            manager.shutdown()

        await query.answer("Bot çalışıyor." if enabled else "Bot durduruldu.")

        counts = permissions.counts()
        active_jobs = len([job for job in manager.jobs.values() if not job.done and not job.cancelled])

        await safe_query_edit(
            query,
            owner_text(
                enabled=counts["enabled"],
                active_jobs=active_jobs,
                banned_users=counts["banned_users"],
                banned_groups=counts["banned_groups"],
            ),
            parse_mode="HTML",
            reply_markup=owner_keyboard(counts["enabled"]),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("post|"):
        parts = data.split("|")
        if len(parts) < 3:
            await query.answer()
            return

        post_id = parts[1]
        action = parts[2]

        posts = context.application.bot_data.setdefault("media_posts", {})
        post = posts.get(post_id)

        if not post:
            await query.answer(t("menu_expired"), show_alert=True)
            return

        # Sade arayüz: tek "Detaylar" butonu. Video bilgisi + açıklama birlikte.
        if action == "details":
            if post.get("details_sent"):
                await query.answer(t("details_sent"), show_alert=True)
                return

            messages = details_messages(post)
            if not messages:
                await query.answer(t("details_none"), show_alert=True)
                return

            post["details_sent"] = True
            await query.answer()

            for index, text in enumerate(messages, start=1):
                await safe_reply(
                    query.message,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_to_message_id=query.message.message_id if index == 1 else None,
                )
            return

        # Geriye dönük uyumluluk: eski info/desc callback'leri
        if action == "info":
            if post.get("info_sent"):
                await query.answer(t("details_sent"), show_alert=True)
                return

            post["info_sent"] = True
            await query.answer()

            await safe_reply(
                query.message,
                media_info_text(post),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_to_message_id=query.message.message_id,
            )
            return

        if action == "desc":
            messages = description_messages(post)

            if not messages:
                await query.answer(t("details_none"), show_alert=True)
                return

            if post.get("desc_sent"):
                await query.answer(t("details_sent"), show_alert=True)
                return

            post["desc_sent"] = True
            await query.answer()

            for index, text in enumerate(messages, start=1):
                await safe_reply(
                    query.message,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_to_message_id=query.message.message_id if index == 1 else None,
                )
            return

    # ── YouTube format menüsü: görünüm değiştir (main/video/audio) ────────────
    if data.startswith("menujob|"):
        await _handle_menujob(update, context)
        return

    # ── YouTube format menüsü: indirmeyi başlat ──────────────────────────────
    if data.startswith("do|"):
        await _handle_do(update, context)
        return

    # ── Playlist aksiyonları ──────────────────────────────────────────────────
    if data.startswith("pl_"):
        await _handle_playlist(update, context)
        return

    await query.answer()


async def _handle_menujob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    if len(parts) < 3:
        await query.answer()
        return

    job_id, view = parts[1], parts[2]
    bot_data = context.application.bot_data

    job = bot_data.get("pending_jobs", {}).get(job_id)
    if not job:
        await query.answer(t("menu_expired_link"), show_alert=True)
        return

    # Not: format/kalite menüsü sadece linki atan kişiye değil, sohbetteki
    # herkese açık — herkes tür (video/ses) ve kalite seçebilir.
    await query.answer()
    view = view if view in {"video", "audio"} else "main"
    keyboard = build_youtube_action_keyboard(job_id, view=view)

    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception:
        try:
            await query.edit_message_caption(
                caption=build_youtube_info_caption(job["info"], job_id),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            try:
                await safe_query_edit(
                    query,
                    build_youtube_info_caption(job["info"], job_id),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass


async def _handle_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    if len(parts) < 3:
        await query.answer()
        return

    job_id, mode = parts[1], parts[2]
    # 4. parça altyazı dilidir: "do|<job>|video_720|tr" → altyazı gömülü indirme
    subtitle_lang = parts[3] if len(parts) > 3 else ""
    bot_data = context.application.bot_data
    permissions = bot_data["permissions"]
    manager = bot_data["process_manager"]
    is_owner = permissions.is_admin(query.from_user.id if query.from_user else None)

    job = bot_data.get("pending_jobs", {}).get(job_id)
    if not job:
        await query.answer(t("menu_expired_link"), show_alert=True)
        return

    # Not: format/kalite seçimi sadece linki atan kişiye değil, sohbetteki
    # herkese açık — grup içindeki herhangi biri indirmeyi başlatabilir.

    # Bakım modu: menü eski olsa bile indirme yapılmaz (admin hariç).
    state = bot_data["bot_state"]
    if state.is_maintenance() and not is_owner:
        await query.answer("Bot şu an bakımda.", show_alert=True)
        return

    if manager.get_user_active_job(job["user_id"]):
        await query.answer(t("wait_active"), show_alert=True)
        return

    await query.answer()

    # Format menüsünü TAMAMEN kaldır (önizleme foto/metin olabilir).
    # Önceden yalnızca butonlar siliniyordu ve menü mesajı sohbette kalıyordu.
    # İş kaydı burada düşürülür; aşağıdaki hata yollarında tekrar denenmez.
    await clear_pending_job(context.application, job_id)

    # Temiz bir durum (status) mesajı gönder — ilerleme bunun üzerinde düzenlenir
    status_msg = None
    try:
        status_msg = await context.bot.send_message(
            chat_id=job["chat_id"],
            text=t("preparing"),
            message_thread_id=job.get("thread_id"),
            reply_to_message_id=job.get("reply_to"),
        )
    except Exception:
        pass

    try:
        new_job = manager.start_download(
            user_id=job["user_id"],
            chat_id=job["chat_id"],
            thread_id=job.get("thread_id"),
            reply_to_message_id=job.get("reply_to"),
            url=job["url"],
            mode=mode,
            username=job.get("username"),
            chat_title=job.get("chat_title"),
            chat_type=job.get("chat_type"),
            subtitle_lang=subtitle_lang,
        )
        if status_msg:
            manager.attach_status_message(new_job.job_id, status_msg.message_id)
    except Exception:
        logger.exception("Format seçimi sonrası indirme başlatılamadı | job=%s", job_id)
        if status_msg:
            try:
                await status_msg.edit_text(t("job_start_failed"))
            except Exception:
                pass

    # Not: menü mesajı ve iş kaydı yukarıda (indirme başlamadan önce)
    # temizlendi; hata yolunda da ekranda kalıntı kalmıyor.


async def _handle_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[0]
    bot_data = context.application.bot_data
    permissions = bot_data["permissions"]
    manager = bot_data["process_manager"]
    is_owner = permissions.is_admin(query.from_user.id if query.from_user else None)

    if action == "pl_noop":
        await query.answer()
        return

    pjob_id = parts[1] if len(parts) >= 2 else ""
    pjob = bot_data.get("playlist_sessions", {}).get(pjob_id)
    if not pjob:
        await query.answer(t("menu_expired_link"), show_alert=True)
        return

    if not (is_owner or (query.from_user and query.from_user.id == pjob.get("user_id"))):
        await query.answer(t("menu_not_yours"), show_alert=True)
        return

    await query.answer()

    async def _refresh_menu() -> None:
        text = build_playlist_header(pjob)
        markup = (
            build_playlist_track_keyboard(pjob, pjob_id)
            if pjob.get("type")
            else _build_playlist_type_keyboard(pjob_id)
        )
        await safe_query_edit(
            query, text, parse_mode="HTML",
            reply_markup=markup, disable_web_page_preview=True,
        )

    if action == "pl_cancel":
        pjob["cancelled"] = True
        bot_data.get("playlist_sessions", {}).pop(pjob_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await safe_reply(query.message, "🛑 " + t("playlist_cancelled"))
        return

    if action == "pl_type" and len(parts) >= 3:
        pjob["type"] = "video" if parts[2] == "video" else "audio"
        pjob["page"] = 0
        await _refresh_menu()
        return

    if action == "pl_page" and len(parts) >= 3:
        try:
            pjob["page"] = int(parts[2])
        except ValueError:
            pass
        await _refresh_menu()
        return

    if action == "pl_sel" and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            return
        sel = pjob.setdefault("selected", set())
        if idx in sel:
            sel.discard(idx)
        else:
            sel.add(idx)
        await _refresh_menu()
        return

    # Bakım/safe modda playlist indirme yapılmaz (admin hariç); gezinme serbest.
    if action in {"pl_item", "pl_dlsel", "pl_dlall"}:
        state = bot_data["bot_state"]
        if (state.is_maintenance() or state.is_safe()) and not is_owner:
            await query.answer("Bot şu an indirme yapamıyor.", show_alert=True)
            return

    if action == "pl_item" and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            return
        if manager.get_user_active_job(pjob["user_id"]) or pjob.get("downloading"):
            await query.answer(t("wait_active"), show_alert=True)
            return
        raw = str(pjob["entries"][idx].get("title") or f"#{idx+1}")[:60]
        status_msg = await context.bot.send_message(
            chat_id=pjob["chat_id"],
            text=f"⬇️ <i>{raw}</i> indiriliyor...",
            parse_mode="HTML",
            message_thread_id=pjob.get("thread_id"),
            reply_to_message_id=pjob.get("reply_to"),
        )
        asyncio.create_task(_run_playlist_download(context, pjob_id, [idx], status_msg))
        return

    if action in {"pl_dlsel", "pl_dlall"}:
        if manager.get_user_active_job(pjob["user_id"]) or pjob.get("downloading"):
            await query.answer(t("wait_active"), show_alert=True)
            return

        if action == "pl_dlsel":
            indices = sorted(pjob.get("selected", set()))
        else:
            limit = 50
            indices = list(range(min(len(pjob["entries"]), limit)))

        if not indices:
            await query.answer("Hiç seçim yok.", show_alert=True)
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        status_msg = await context.bot.send_message(
            chat_id=pjob["chat_id"],
            text=f"⏳ {len(indices)} içerik indiriliyor...",
            parse_mode="HTML",
            message_thread_id=pjob.get("thread_id"),
            reply_to_message_id=pjob.get("reply_to"),
        )
        asyncio.create_task(_run_playlist_download(context, pjob_id, indices, status_msg))
        return
