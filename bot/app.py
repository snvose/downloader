from __future__ import annotations

import asyncio
import logging
import os
import time

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import Config
from .emoji_manager import ensure_file
from .handlers.admin import (
    admin_command,
    admin_callback,
    broadcast_compose_message,
    banid_command,
    basla_command,
    dur_command,
    refresh_command,
    status_command,
    unbanid_command,
)
from .handlers.buttons import button_handler
from .handlers.gate import ban_gate
from .handlers.commands import (
    cancel_command,
    duyuru_command,
    help_command,
    ses_command,
    start_command,
)
from .handlers.emoji_admin import emoji_command, emoji_detect_message, emojiler_command
from .handlers.links import link_handler
from .i18n import t
from .logger import setup_logging
from .permissions import Permissions
from .process_manager import ProcessManager
from .safe_message import is_entity_error, strip_custom_emoji
from .sender import FloodLimitError, cleanup_old_posts, send_downloaded_files
from .storage import increment_stat, init_runtime_files
from .ui import cancelled_text, configure_branding, progress_text, uploading_text
from .utils import safe_public_error
# YENİ modüller: cache, chat takibi, mod yönetimi, temizlik, loglama, bildirim
from .cache import MediaCache
from .chats import ChatRegistry
from .analytics import ActivityBuffer, activity_flusher
from .cookie_health import CookieLog
from .db import Database
from .db_migrate import migrate_json_to_db
from .live_guard import LiveGuard, guard_message
from .pending import expire_pending_jobs
from .state import BotState
from .scheduler import cleanup_scheduler
from .download_log import log_download, log_download_error
from .admin_notify import notify_admin_failure


logger = logging.getLogger("downloader")


async def edit_job_message(
    app: Application,
    job,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    if not job.status_message_id:
        return

    try:
        await app.bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if not is_entity_error(exc):
            return

        try:
            await app.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=strip_custom_emoji(text),
                parse_mode="HTML" if parse_mode else None,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


def _cleanup_runtime(app: Application, manager: ProcessManager, job_id: str, *, keep_files: bool = False) -> None:
    cleanup_old_posts(app.bot_data.setdefault("media_posts", {}))
    if keep_files:
        # başarılı indirmede dosyalar diskte kalır (cache/günlük temizlik için)
        manager.detach_job(job_id)
    else:
        manager.remove_job(job_id)


async def handle_watchdog_kill(
    app: Application,
    manager: ProcessManager,
    job,
    reason: str,
    config: Config | None,
) -> None:
    """
    Watchdog tarafından öldürülen işi kullanıcıya bildirir ve kaydını temizler.

    Worker öldürüldüğü için kuyruğa "error" olayı gelmez; bildirimi burada
    yapmazsak kullanıcı sonsuza dek "Hazırlanıyor..." mesajına bakar.
    """
    messages = {
        "timeout": t("job_timeout"),
        "oversize": t("job_oversize"),
        "dead": t("job_failed_generic"),
    }
    text = messages.get(reason, t("job_failed_generic"))

    if not job.silent:
        await edit_job_message(app, job, text, parse_mode="HTML")

    await asyncio.to_thread(
        log_download,
        user_id=job.user_id, username=job.username,
        chat_id=job.chat_id, chat_title=job.chat_title,
        chat_type=job.chat_type, platform=None,
        url=job.source_url, result=f"watchdog_{reason}",
        duration=time.time() - job.started_at,
    )

    if config:
        increment_stat(config.data_dir, "cancelled_downloads")

    _cleanup_runtime(app, manager, job.job_id)


async def queue_consumer(app: Application) -> None:
    manager: ProcessManager = app.bot_data["process_manager"]
    config: Config | None = app.bot_data.get("config")

    last_reap = 0.0
    last_pending_sweep = 0.0

    while True:
        try:
            # ── Watchdog ──────────────────────────────────────────────────────
            # Sınırı aşan işleri (süre/boyut/ölü süreç) öldürür. Boşta kalan
            # döngüde saniyede bir yeterli; iş yoksa hiç maliyeti yok.
            now_ts = time.time()
            if manager.jobs and now_ts - last_reap >= 1.0:
                last_reap = now_ts
                for dead_job, reason in manager.reap():
                    await handle_watchdog_kill(app, manager, dead_job, reason, config)

            # Süresi dolan format menülerini temizle (dakikada bir yeterli).
            if now_ts - last_pending_sweep >= 60.0:
                last_pending_sweep = now_ts
                try:
                    await expire_pending_jobs(app)
                except Exception:
                    logger.exception("Bekleyen menü temizliği başarısız")

            event = manager.get_event_nowait()

            if event is None:
                await asyncio.sleep(0.25)
                continue

            event_type = event.get("type")
            job_id = event.get("job_id")
            job = manager.jobs.get(job_id)

            if not job:
                continue

            if event_type == "log":
                logger.info("JOB %s | %s", job_id, event.get("message"))
                continue

            if event_type == "cookie_error":
                # Cookie kaynaklı hata: ayrı log kanalı + panel sayacı.
                cookie_log: CookieLog | None = app.bot_data.get("cookie_log")
                if cookie_log:
                    await asyncio.to_thread(
                        cookie_log.record,
                        platform=str(event.get("platform") or ""),
                        reason=str(event.get("reason") or ""),
                        url=str(event.get("url") or ""),
                        error=str(event.get("error") or ""),
                        user_id=job.user_id,
                    )
                    logger.warning(
                        "COOKIE %s | platform=%s | sebep=%s",
                        job_id, event.get("platform"), event.get("reason"),
                    )
                continue

            if event_type == "progress":
                # Safe mode (silent): ilerleme/yazıyor bildirimi gösterilmez
                if job.silent:
                    continue
                now = time.time()
                if now - job.last_edit_at < 1.2:
                    continue

                job.last_edit_at = now
                await edit_job_message(
                    app,
                    job,
                    progress_text(event),
                    parse_mode="HTML",
                )
                continue

            if event_type == "done":
                files = event.get("files") or []
                title = event.get("title") or ""
                source_url = event.get("source_url") or job.source_url
                info = event.get("info") or {}
                mode = event.get("mode") or job.mode

                job.title = title
                manager.mark_done(job_id)

                # Safe mode (silent): yükleniyor mesajı/edit YOK
                if not job.silent:
                    await edit_job_message(
                        app,
                        job,
                        uploading_text(),
                        parse_mode="HTML",
                    )

                cache: MediaCache | None = app.bot_data.get("media_cache")
                chats: ChatRegistry | None = app.bot_data.get("chat_registry")

                try:
                    sent_items = await send_downloaded_files(
                        context=app,
                        chat_id=job.chat_id,
                        thread_id=job.thread_id,
                        reply_to_message_id=job.reply_to_message_id,
                        files=files,
                        title=title,
                        source_url=source_url,
                        info=info,
                        mode=mode,
                        user_id=job.user_id,
                        bare=job.silent,  # safe mode → başlık/buton yok
                    )
                except FloodLimitError as exc:
                    logger.exception("JOB %s SEND ERROR (flood limit, retry_after=%ss)", job_id, exc.retry_after)
                    if not job.silent:
                        await edit_job_message(
                            app,
                            job,
                            f"⏳ Telegram şu anda çok yoğun (flood limiti). "
                            f"Lütfen birkaç dakika sonra tekrar deneyin.",
                        )
                    if config:
                        increment_stat(config.data_dir, "failed_downloads")
                    await asyncio.to_thread(
                        log_download,
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=info.get("platform"),
                        url=source_url, result="flood_limit",
                        duration=time.time() - job.started_at,
                    )
                    if config:
                        await notify_admin_failure(
                            app.bot, config.admin_id,
                            summary=f"Telegram flood limiti aşıldı (retry_after={exc.retry_after}s).",
                            url=source_url, platform=str(info.get("platform") or ""),
                            user_id=job.user_id, username=job.username,
                            chat_id=job.chat_id, chat_title=job.chat_title,
                        )
                    _cleanup_runtime(app, manager, job_id)
                    continue
                except Exception:
                    logger.exception("JOB %s SEND ERROR", job_id)
                    if not job.silent:
                        await edit_job_message(
                            app,
                            job,
                            "Dosyalar Telegram'a yüklenemedi. Sunucu veya Telegram API hatası olabilir.",
                        )
                    if config:
                        increment_stat(config.data_dir, "failed_downloads")
                    # Detaylı loglama (hata)
                    await asyncio.to_thread(
                        log_download,
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=info.get("platform"),
                        url=source_url, result="send_failed",
                        duration=time.time() - job.started_at,
                    )
                    # Admine bildirim (safe modda da admin bilgilendirilir)
                    if config:
                        await notify_admin_failure(
                            app.bot, config.admin_id,
                            summary="Telegram'a yükleme başarısız.",
                            url=source_url, platform=str(info.get("platform") or ""),
                            user_id=job.user_id, username=job.username,
                            chat_id=job.chat_id, chat_title=job.chat_title,
                        )
                    _cleanup_runtime(app, manager, job_id)
                    continue

                # file_id'leri cache'e yaz. Yalnızca lookup ile eşleşen modlar
                # (auto/audio_best) cache'lenir; interaktif YouTube formatları
                # (video_1080, thumbnail vb.) hiç okunmadığı için bloat yapmasın.
                # Eksik (too-large nedeniyle atlanmış) gönderimler cache'lenmez.
                cacheable = mode in {"auto", "audio_best"}
                complete = len(sent_items) == len(files)
                if cache and sent_items and cacheable and complete:
                    await asyncio.to_thread(
                        cache.store, source_url, mode, sent_items,
                        title=title, info=info,
                    )

                # başarılı indirmede sohbet kullanım kaydını güncelle
                if chats:
                    await asyncio.to_thread(
                        chats.record_download,
                        chat_id=job.chat_id,
                        title=job.chat_title or "",
                        chat_type=job.chat_type or "",
                        platform=str(info.get("platform") or ""),
                    )

                # Detaylı indirme logu (tüm alanlar). Boyut hesabı blocking
                # olduğundan log ile birlikte thread'e taşınır.
                db: Database | None = app.bot_data.get("db")

                def _log_success() -> None:
                    total_size = 0
                    for fp in files:
                        try:
                            total_size += os.path.getsize(fp)
                        except OSError:
                            pass

                    # Veritabanı kaydı: kullanıcı/sohbet sayaçları + geçmiş.
                    # (JSON logu geriye dönük uyumluluk için korunuyor.)
                    if db:
                        try:
                            db.record_download(
                                user_id=job.user_id, chat_id=job.chat_id,
                                platform=str(info.get("platform") or ""),
                                url=source_url, mode=mode,
                                source=str(info.get("source") or ""),
                                result="success", file_size=total_size,
                                duration=time.time() - job.started_at,
                                username=job.username, chat_title=job.chat_title,
                                chat_type=job.chat_type,
                            )
                        except Exception:
                            logger.exception("DB indirme kaydı başarısız")

                    log_download(
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=info.get("platform"),
                        url=source_url, result="success",
                        file_size=total_size, duration=time.time() - job.started_at,
                    )
                await asyncio.to_thread(_log_success)

                # Durum mesajını sil (yalnızca normal modda ve mesaj varsa)
                if not job.silent and job.status_message_id:
                    try:
                        await app.bot.delete_message(
                            chat_id=job.chat_id,
                            message_id=job.status_message_id,
                        )
                    except Exception:
                        pass

                if config:
                    increment_stat(config.data_dir, "total_downloads", info.get("platform"))

                # başarı → dosyaları koru (cache disk-fallback + günlük temizlik)
                _cleanup_runtime(app, manager, job_id, keep_files=True)
                continue

            if event_type == "error":
                manager.mark_done(job_id)

                public_message = event.get("public_message") or event.get("error") or ""
                kind = event.get("kind") or "generic"

                # ── Canlı yayın: beklenen reddetme, arıza değil ────────────────
                # Kullanıcıya net mesaj + strike; admine hata bildirimi YOK.
                if kind == "live":
                    guard = app.bot_data.get("live_guard")
                    perms = app.bot_data.get("permissions")
                    text = t("live_not_supported")

                    if guard and not (perms and perms.is_admin(job.user_id)):
                        result = await asyncio.to_thread(guard.register_attempt, job.user_id)
                        text = guard_message(result)

                    if not job.silent:
                        await edit_job_message(app, job, text, parse_mode="HTML")

                    logger.warning("JOB %s canlı yayın reddedildi | url=%s", job_id, job.source_url)
                    await asyncio.to_thread(
                        log_download,
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=None,
                        url=job.source_url, result="live_rejected",
                        duration=time.time() - job.started_at,
                    )
                    _cleanup_runtime(app, manager, job_id)
                    continue

                # Safe mode (silent): kullanıcıya HİÇBİR bildirim gönderilmez
                if not job.silent:
                    await edit_job_message(
                        app,
                        job,
                        safe_public_error(public_message),
                    )

                # B-yeni: tüm hatalar tam traceback ile loglanır
                logger.error("JOB %s ERROR\n%s", job_id, event.get("error"))
                await asyncio.to_thread(
                    log_download_error,
                    url=job.source_url,
                    platform=None,
                    traceback_text=str(event.get("error") or ""),
                )
                await asyncio.to_thread(
                    log_download,
                    user_id=job.user_id, username=job.username,
                    chat_id=job.chat_id, chat_title=job.chat_title,
                    chat_type=job.chat_type, platform=None,
                    url=job.source_url, result="error",
                    duration=time.time() - job.started_at,
                )

                if config:
                    increment_stat(config.data_dir, "failed_downloads")
                    # indirme tamamlanamadı → admine özet + son 20 satır + 2 buton
                    await notify_admin_failure(
                        app.bot, config.admin_id,
                        summary=str(public_message)[:300] or "Bilinmeyen indirme hatası.",
                        url=job.source_url, platform="",
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                    )
                _cleanup_runtime(app, manager, job_id)
                continue

            if event_type == "cancelled":
                manager.mark_done(job_id)
                if not job.silent:
                    await edit_job_message(
                        app,
                        job,
                        cancelled_text(),
                        parse_mode="HTML",
                    )
                if config:
                    increment_stat(config.data_dir, "cancelled_downloads")
                _cleanup_runtime(app, manager, job_id)
                continue

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Queue consumer hatası")
            await asyncio.sleep(1)


async def app_error_handler(update: object, context) -> None:
    logger.exception("Uygulama hatası", exc_info=context.error)

    try:
        config = context.application.bot_data.get("config")
        if config and config.admin_id:
            await context.bot.send_message(
                chat_id=config.admin_id,
                text=f"Uygulama hatası:\n{str(context.error)[:3500]}",
            )
    except Exception:
        pass


async def post_init(app: Application) -> None:
    app.bot_data["queue_task"] = asyncio.create_task(queue_consumer(app))
    logger.info("Queue consumer başlatıldı.")

    # Mevcut JSON geçmişini veritabanına taşı (bir kez; tekrarı güvenli).
    db: Database | None = app.bot_data.get("db")
    cfg: Config | None = app.bot_data.get("config")
    if db and cfg:
        try:
            counts = await asyncio.to_thread(migrate_json_to_db, db, cfg.data_dir)
            if not counts.get("skipped"):
                logger.info(
                    "JSON → DB taşındı: %s sohbet, %s kullanıcı, %s platform kaydı",
                    counts["chats"], counts["users"], counts["platforms"],
                )
        except Exception:
            logger.exception("JSON → DB taşıma başarısız (bot çalışmaya devam ediyor)")

    # günlük downloads temizliği (asyncio, harici cron yok)
    config: Config | None = app.bot_data.get("config")
    cache: MediaCache | None = app.bot_data.get("media_cache")
    if config and cache:
        app.bot_data["cleanup_task"] = asyncio.create_task(
            cleanup_scheduler(config, cache)
        )
        logger.info("Temizlik zamanlayıcı başlatıldı.")

    # Aktivite tamponu: her mesajda DB'ye yazmak yerine periyodik toplu yazım.
    buffer = app.bot_data.get("activity_buffer")
    if buffer:
        app.bot_data["activity_task"] = asyncio.create_task(activity_flusher(buffer))
        logger.info("Aktivite tamponu başlatıldı.")


async def post_shutdown(app: Application) -> None:
    for task_key in ("queue_task", "cleanup_task", "activity_task"):
        task = app.bot_data.get(task_key)
        if task:
            task.cancel()

    manager: ProcessManager | None = app.bot_data.get("process_manager")
    if manager:
        manager.close()  # queue'yu da kapat (yalnızca tam kapanışta)


def build_application(config: Config) -> Application:
    builder = (
        Application.builder()
        .token(config.bot_token)
        .write_timeout(600.0)
        .read_timeout(600.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )

    if config.local_bot_api_base:
        root = config.local_bot_api_base.rstrip("/")
        builder = (
            builder
            .base_url(root + "/bot")
            .base_file_url(root + "/file/bot")
            .local_mode(True)
        )

    app = builder.build()
    app.bot_data["config"] = config
    app.bot_data["process_manager"] = ProcessManager(config)
    app.bot_data["permissions"] = Permissions(config)
    app.bot_data["media_posts"] = {}
    # YENİ bileşenler: cache, sohbet kaydı, mod durumu
    app.bot_data["media_cache"] = MediaCache(config.data_dir, enabled=config.cache_enabled)
    app.bot_data["chat_registry"] = ChatRegistry(config.data_dir)
    app.bot_data["bot_state"] = BotState(config.data_dir)
    app.bot_data["db"] = Database(config.db_path)
    app.bot_data["cookie_log"] = CookieLog(config.data_dir, config.log_dir)
    app.bot_data["activity_buffer"] = ActivityBuffer(app.bot_data["db"])
    app.bot_data["live_guard"] = LiveGuard(
        config.data_dir,
        strike_limit=config.live_strike_limit,
        ban_days=config.live_ban_days,
    )
    # Tutarlılık için koleksiyon anahtarlarını baştan oluştur
    app.bot_data["pending_jobs"] = {}
    app.bot_data["playlist_sessions"] = {}

    # Ban kapısı: her şeyden önce (group=-2). Banlı kullanıcı/grup buradan
    # geçemez; böylece yeni bir handler eklendiğinde ban kontrolünü unutmak
    # mümkün olmuyor. Bkz. bot/handlers/gate.py
    app.add_handler(TypeHandler(Update, ban_gate), group=-2)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("ses", ses_command))
    app.add_handler(CommandHandler("duyurular", duyuru_command))

    app.add_handler(CommandHandler("dur", dur_command))
    app.add_handler(CommandHandler("basla", basla_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("banid", banid_command))
    app.add_handler(CommandHandler("unbanid", unbanid_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("admin", admin_command))  # mod + kullanım paneli

    app.add_handler(CommandHandler("emojiler", emojiler_command))
    app.add_handler(CommandHandler("emoji", emoji_command))

    # admin mod butonları (Safe/Maintenance) — diğer callback'lerden önce
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin\|"))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Duyuru taslağı: admin "Mesaj Yaz" dedikten sonraki mesajı yakalar.
    # group=-1 → link handler'dan ÖNCE çalışır, yoksa duyuru metni indirme
    # isteği sanılırdı.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                       broadcast_compose_message),
        group=-1,
    )

    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, link_handler), group=0)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, emoji_detect_message), group=1)

    app.add_error_handler(app_error_handler)

    return app


def run_bot(config: Config) -> None:
    setup_logging(config)
    init_runtime_files(config.data_dir)
    ensure_file()
    configure_branding(config)  # owner/topluluk linklerini .env'den UI'a aktar

    app = build_application(config)

    # Kayıtlı bot dilini i18n'e yükle
    from .i18n import set_language
    set_language(BotState(config.data_dir).get_language())

    logger.info("%s başlatılıyor...", config.bot_name)
    logger.info("Data dizini: %s", config.data_dir)
    logger.info("Download dizini: %s", config.download_dir)
    logger.info("Local Bot API: %s", "aktif" if config.local_bot_api_base else "kapalı")

    app.run_polling(
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=20,
    )
