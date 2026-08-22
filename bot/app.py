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
from .utils import is_no_media_error, platform_name, safe_public_error
from .cache import MediaCache
from .chats import ChatRegistry
from .analytics import ActivityBuffer, activity_flusher
from .cookie_health import CookieLog
from .db import Database
from .db_migrate import migrate_json_to_db
from .live_guard import LiveGuard, guard_message
from .pending import expire_pending_jobs
from .state import BotState
from .cookie_watch import cookie_watch_scheduler
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
        # Files stay on disk after a success (cache fallback, daily cleanup).
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
    Notifies the user about a job the watchdog killed and cleans up its record.

    The worker was killed, so no "error" event ever reaches the queue; without
    this the user would stare at "Preparing..." forever.
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
        chat_type=job.chat_type,
        platform=platform_name(job.source_url),
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
            # Kills jobs over their limit (time/size/dead process). Once a
            # second is plenty when idle and costs nothing when there are no
            # jobs.
            now_ts = time.time()
            if manager.jobs and now_ts - last_reap >= 1.0:
                last_reap = now_ts
                for dead_job, reason in manager.reap():
                    await handle_watchdog_kill(app, manager, dead_job, reason, config)

            # Expired format menus, once a minute is enough.
            if now_ts - last_pending_sweep >= 60.0:
                last_pending_sweep = now_ts
                try:
                    await expire_pending_jobs(app)
                except Exception:
                    logger.exception("Pending menu cleanup failed")

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
                        "COOKIE %s | platform=%s | reason=%s",
                        job_id, event.get("platform"), event.get("reason"),
                    )
                continue

            if event_type == "progress":
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
                        bare=job.silent,
                    )
                except FloodLimitError as exc:
                    logger.exception("JOB %s SEND ERROR (flood limit, retry_after=%ss)", job_id, exc.retry_after)
                    if not job.silent:
                        await edit_job_message(app, job, t("flood_limit"))
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
                            summary=f"Telegram flood limit hit (retry_after={exc.retry_after}s).",
                            url=source_url, platform=str(info.get("platform") or ""),
                            user_id=job.user_id, username=job.username,
                            chat_id=job.chat_id, chat_title=job.chat_title,
                        )
                    _cleanup_runtime(app, manager, job_id)
                    continue
                except Exception:
                    logger.exception("JOB %s SEND ERROR", job_id)
                    if not job.silent:
                        await edit_job_message(app, job, t("upload_failed"))
                    if config:
                        increment_stat(config.data_dir, "failed_downloads")
                    await asyncio.to_thread(
                        log_download,
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=info.get("platform"),
                        url=source_url, result="send_failed",
                        duration=time.time() - job.started_at,
                    )
                    if config:
                        await notify_admin_failure(
                            app.bot, config.admin_id,
                            summary="Upload to Telegram failed.",
                            url=source_url, platform=str(info.get("platform") or ""),
                            user_id=job.user_id, username=job.username,
                            chat_id=job.chat_id, chat_title=job.chat_title,
                        )
                    _cleanup_runtime(app, manager, job_id)
                    continue

                # Only lookup-friendly modes (auto/audio_best) are cached;
                # interactive YouTube formats (video_1080, thumbnail...) are
                # never looked up again, so caching them would just be bloat.
                # A partial send (files skipped for size) is not cached either.
                cacheable = mode in {"auto", "audio_best"}
                complete = len(sent_items) == len(files)
                if cache and sent_items and cacheable and complete:
                    await asyncio.to_thread(
                        cache.store, source_url, mode, sent_items,
                        title=title, info=info,
                    )

                if chats:
                    await asyncio.to_thread(
                        chats.record_download,
                        chat_id=job.chat_id,
                        title=job.chat_title or "",
                        chat_type=job.chat_type or "",
                        platform=str(info.get("platform") or ""),
                    )

                # Detailed download log (all fields). Size calculation is
                # blocking, so it moves to a thread along with the log write.
                db: Database | None = app.bot_data.get("db")

                def _log_success() -> None:
                    total_size = 0
                    for fp in files:
                        try:
                            total_size += os.path.getsize(fp)
                        except OSError:
                            pass

                    # Database record: user/chat counters + history. The JSON
                    # log is kept alongside it for backward compatibility.
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
                            logger.exception("DB download record failed")

                    log_download(
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type, platform=info.get("platform"),
                        url=source_url, result="success",
                        file_size=total_size, duration=time.time() - job.started_at,
                    )
                await asyncio.to_thread(_log_success)

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

                _cleanup_runtime(app, manager, job_id, keep_files=True)
                continue

            if event_type == "error":
                manager.mark_done(job_id)

                public_message = event.get("public_message") or event.get("error") or ""
                kind = event.get("kind") or "generic"

                # ── Livestream: an expected rejection, not a malfunction ──────
                # The user gets a clear message plus a strike; no admin alert.
                if kind == "live":
                    guard = app.bot_data.get("live_guard")
                    perms = app.bot_data.get("permissions")
                    text = t("live_not_supported")

                    if guard and not (perms and perms.is_admin(job.user_id)):
                        result = await asyncio.to_thread(guard.register_attempt, job.user_id)
                        text = guard_message(result)

                    if not job.silent:
                        await edit_job_message(app, job, text, parse_mode="HTML")

                    logger.warning("JOB %s livestream rejected | url=%s", job_id, job.source_url)
                    await asyncio.to_thread(
                        log_download,
                        user_id=job.user_id, username=job.username,
                        chat_id=job.chat_id, chat_title=job.chat_title,
                        chat_type=job.chat_type,
                        platform=platform_name(job.source_url),
                        url=job.source_url, result="live_rejected",
                        duration=time.time() - job.started_at,
                    )
                    _cleanup_runtime(app, manager, job_id)
                    continue

                if not job.silent:
                    await edit_job_message(
                        app,
                        job,
                        safe_public_error(public_message),
                    )

                # A post with nothing to download is a normal outcome, not a
                # malfunction: the user is told, but it is not worth a
                # traceback in the log or an alert to the admin.
                no_media = is_no_media_error(public_message)

                if no_media:
                    logger.info("JOB %s: no media in post | url=%s", job_id, job.source_url)
                else:
                    # All errors are logged with a full traceback.
                    logger.error("JOB %s ERROR\n%s", job_id, event.get("error"))

                if not no_media:
                    await asyncio.to_thread(
                        log_download_error,
                        url=job.source_url,
                        platform=platform_name(job.source_url),
                        traceback_text=str(event.get("error") or ""),
                    )
                await asyncio.to_thread(
                    log_download,
                    user_id=job.user_id, username=job.username,
                    chat_id=job.chat_id, chat_title=job.chat_title,
                    chat_type=job.chat_type,
                    platform=platform_name(job.source_url),
                    url=job.source_url,
                    result="no_media" if no_media else "error",
                    duration=time.time() - job.started_at,
                )

                if config and not no_media:
                    increment_stat(config.data_dir, "failed_downloads")
                    await notify_admin_failure(
                        app.bot, config.admin_id,
                        summary=str(public_message)[:300] or "Unknown download error.",
                        url=job.source_url,
                        platform=platform_name(job.source_url),
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
            logger.exception("Queue consumer error")
            await asyncio.sleep(1)


async def app_error_handler(update: object, context) -> None:
    logger.exception("Application error", exc_info=context.error)

    try:
        config = context.application.bot_data.get("config")
        if config and config.admin_id:
            await context.bot.send_message(
                chat_id=config.admin_id,
                text=f"Application error:\n{str(context.error)[:3500]}",
            )
    except Exception:
        pass


async def post_init(app: Application) -> None:
    app.bot_data["queue_task"] = asyncio.create_task(queue_consumer(app))
    logger.info("Queue consumer started.")

    # Migrate legacy JSON history into the database (once; safe to repeat).
    db: Database | None = app.bot_data.get("db")
    cfg: Config | None = app.bot_data.get("config")
    if db and cfg:
        try:
            counts = await asyncio.to_thread(migrate_json_to_db, db, cfg.data_dir)
            if not counts.get("skipped"):
                logger.info(
                    "JSON -> DB migration: %s chats, %s users, %s platform rows",
                    counts["chats"], counts["users"], counts["platforms"],
                )
        except Exception:
            logger.exception("JSON -> DB migration failed (bot keeps running)")

    # Daily downloads cleanup (asyncio, no external cron).
    config: Config | None = app.bot_data.get("config")
    cache: MediaCache | None = app.bot_data.get("media_cache")
    manager: ProcessManager | None = app.bot_data.get("process_manager")
    if config and cache and manager:
        app.bot_data["cleanup_task"] = asyncio.create_task(
            cleanup_scheduler(config, cache, manager.active_download_dirs)
        )
        logger.info("Cleanup scheduler started.")

    # Hourly cookie check + one detailed report a day to the admin.
    if config:
        app.bot_data["cookie_watch_task"] = asyncio.create_task(
            cookie_watch_scheduler(app.bot, config)
        )
        logger.info("Cookie watch started.")

    # Activity buffer: batches writes instead of hitting the DB on every message.
    buffer = app.bot_data.get("activity_buffer")
    if buffer:
        app.bot_data["activity_task"] = asyncio.create_task(activity_flusher(buffer))
        logger.info("Activity buffer flusher started.")


async def post_shutdown(app: Application) -> None:
    for task_key in ("queue_task", "cleanup_task", "activity_task", "cookie_watch_task"):
        task = app.bot_data.get(task_key)
        if task:
            task.cancel()

    manager: ProcessManager | None = app.bot_data.get("process_manager")
    if manager:
        manager.close()  # closes the queue too (full shutdown only)


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
    app.bot_data["pending_jobs"] = {}
    app.bot_data["playlist_sessions"] = {}

    # Ban gate: runs before everything else (group=-2). A banned user/group
    # cannot get past this, so a new handler can never forget the ban check.
    # See bot/handlers/gate.py
    app.add_handler(TypeHandler(Update, ban_gate), group=-2)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("audio", ses_command))
    app.add_handler(CommandHandler("broadcasts", duyuru_command))

    app.add_handler(CommandHandler("stop", dur_command))
    app.add_handler(CommandHandler("resume", basla_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("banid", banid_command))
    app.add_handler(CommandHandler("unbanid", unbanid_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CommandHandler("emojis", emojiler_command))
    app.add_handler(CommandHandler("emoji", emoji_command))

    # Admin mode buttons (Safe/Maintenance) — before the other callbacks.
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin\|"))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Broadcast draft: catches the admin's next message after "Write message".
    # group=-1 runs it BEFORE the link handler, otherwise the draft text would
    # be treated as a download request.
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
    configure_branding(config)  # copies owner/community links from .env into the UI

    app = build_application(config)

    # Load the persisted bot language into i18n.
    from .i18n import set_language
    set_language(BotState(config.data_dir).get_language())

    logger.info("%s starting...", config.bot_name)
    logger.info("Data directory: %s", config.data_dir)
    logger.info("Download directory: %s", config.download_dir)
    logger.info("Local Bot API: %s", "on" if config.local_bot_api_base else "off")

    app.run_polling(
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=20,
    )
