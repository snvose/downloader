from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from telegram import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, RetryAfter

from .i18n import t
from .safe_message import is_entity_error, strip_custom_emoji
from .ui import build_post_keyboard, final_caption
from .utils import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS, human_bytes


logger = logging.getLogger("downloader")
MAX_BYTES_NO_LOCAL_API = 50 * 1024 * 1024
MEDIA_GROUP_LIMIT = 10              # Telegram sendMediaGroup limit
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # hard limit for the "photo" type

# Flood control (RetryAfter) retry budget.
FLOOD_MAX_ATTEMPTS = 4
FLOOD_MAX_TOTAL_WAIT = 180  # seconds


class FloodLimitError(RuntimeError):
    """Telegram flood limit; still blocked after every retry."""

    def __init__(self, retry_after: int):
        self.retry_after = int(retry_after)
        super().__init__(f"Telegram flood limit hit, retry after {self.retry_after}s.")


async def _with_flood_retry(coro_factory):
    """Waits and retries on RetryAfter, then raises FloodLimitError."""
    total_wait = 0.0
    last_retry_after = 5

    for attempt in range(FLOOD_MAX_ATTEMPTS):
        try:
            return await coro_factory()
        except RetryAfter as exc:
            last_retry_after = int(getattr(exc, "retry_after", 5) or 5)
            wait = last_retry_after + 1
            total_wait += wait

            if attempt >= FLOOD_MAX_ATTEMPTS - 1 or total_wait > FLOOD_MAX_TOTAL_WAIT:
                raise FloodLimitError(last_retry_after) from exc

            logger.warning(
                "Flood control: waiting %ss (attempt %s/%s)",
                wait, attempt + 1, FLOOD_MAX_ATTEMPTS,
            )
            await asyncio.sleep(wait)

    raise FloodLimitError(last_retry_after)


def _is_reply_not_found(exc: Exception) -> bool:
    # Telegram returns this when the user deleted their original message.
    raw = str(exc).lower()
    return "message to be replied not found" in raw or "reply message not found" in raw


def _extract_file_id(message: Any, kind: str) -> str | None:
    """Grabs the file_id of a sent message so it can be reused from cache."""
    if message is None:
        return None
    try:
        if kind == "video" and message.video:
            return message.video.file_id
        if kind == "photo" and message.photo:
            return message.photo[-1].file_id  # highest resolution
        if kind == "audio" and message.audio:
            return message.audio.file_id
        if kind == "document" and message.document:
            return message.document.file_id
        # Telegram may return a video as a document or animation.
        if getattr(message, "document", None):
            return message.document.file_id
        if getattr(message, "video", None):
            return message.video.file_id
    except Exception:
        return None
    return None


def _strip_caption_kwargs(kwargs: dict) -> dict:
    clean = dict(kwargs)
    if clean.get("caption"):
        clean["caption"] = strip_custom_emoji(str(clean["caption"]))
        clean["parse_mode"] = "HTML"
    return clean


def _local_api_enabled(context: Any) -> bool:
    try:
        config = context.bot_data.get("config")
        return bool(config and config.local_bot_api_base)
    except Exception:
        return False


def _max_upload_bytes(context: Any) -> int:
    if not _local_api_enabled(context):
        return MAX_BYTES_NO_LOCAL_API

    try:
        config = context.bot_data.get("config")
        mb = int(getattr(config, "max_file_size_mb", 1900) or 1900)
        return mb * 1024 * 1024
    except Exception:
        return 1900 * 1024 * 1024


def _total_size(files: list[str]) -> int:
    total = 0
    for file_path in files:
        try:
            total += Path(file_path).stat().st_size
        except OSError:
            pass
    return total


def cleanup_old_posts(store: dict, max_age_seconds: int = 3600) -> None:
    now = time.time()
    old_keys = [
        key for key, value in list(store.items())
        if now - float(value.get("created_at", now)) > max_age_seconds
    ]

    for key in old_keys:
        store.pop(key, None)


def _create_media_post(
    context: Any,
    *,
    info: dict,
    source_url: str,
    title: str,
    mode: str,
    files: list[str],
    user_id: int | None,
) -> str:
    store = context.bot_data.setdefault("media_posts", {})
    cleanup_old_posts(store)

    post_id = uuid.uuid4().hex[:12]
    store[post_id] = {
        "id": post_id,
        "created_at": time.time(),
        "info": info or {},
        "url": source_url,
        "title": title,
        "mode": mode,
        "files": files,
        "file_count": len(files),
        "total_size": _total_size(files),
        "user_id": int(user_id or 0),
        "desc_sent": False,
        "info_sent": False,
    }

    return post_id


async def _send_too_large_message(
    *,
    context: Any,
    chat_id: int,
    thread_id: int | None,
    reply_to_message_id: int | None,
    path: Path,
    file_size: int,
    max_bytes: int,
) -> None:
    reason = (
        f"Local Bot API: {human_bytes(max_bytes)}"
        if _local_api_enabled(context)
        else "Telegram Bot API < 50 MB"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        reply_to_message_id=reply_to_message_id,
        text="⚠️ " + t("too_large", name=path.name, size=human_bytes(file_size), reason=reason),
    )


async def _call_with_reply_fallback(send_coro_factory, common: dict):
    # Retries without reply_to_message_id when the replied message is gone.
    try:
        return await _with_flood_retry(lambda: send_coro_factory(common))
    except BadRequest as exc:
        if _is_reply_not_found(exc) and common.get("reply_to_message_id") is not None:
            retry_common = dict(common)
            retry_common["reply_to_message_id"] = None
            logger.warning("Reply target missing, retrying without a reply.")
            return await _with_flood_retry(lambda: send_coro_factory(retry_common))
        raise


async def _send_video(context: Any, path: Path, common: dict, caption: str | None, keyboard) -> Any:
    kwargs = {
        "caption": caption,
        "parse_mode": "HTML" if caption else None,
        "reply_markup": keyboard,
        "supports_streaming": True,
    }

    if _local_api_enabled(context):
        try:
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_video(video=str(path), **kwargs, **c), common
            )
        except FloodLimitError:
            # Re-uploading the same file through the cloud API would only make
            # the flood limit worse.
            raise
        except Exception as local_exc:
            logger.warning("Local API send_video failed, falling back: %s", local_exc)

    with path.open("rb") as file:
        async def _do(c):
            file.seek(0)
            return await context.bot.send_video(video=file, **kwargs, **c)
        try:
            return await _call_with_reply_fallback(_do, common)
        except BadRequest as exc:
            if not is_entity_error(exc):
                raise
            file.seek(0)
            clean = _strip_caption_kwargs(kwargs)
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_video(video=file, **clean, **c), common
            )


async def _send_photo(context: Any, path: Path, common: dict, caption: str | None, keyboard) -> Any:
    kwargs = {
        "caption": caption,
        "parse_mode": "HTML" if caption else None,
        "reply_markup": keyboard,
    }

    if _local_api_enabled(context):
        try:
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_photo(photo=str(path), **kwargs, **c), common
            )
        except FloodLimitError:
            raise
        except Exception as local_exc:
            logger.warning("Local API send_photo failed, falling back: %s", local_exc)

    with path.open("rb") as file:
        async def _do(c):
            file.seek(0)
            return await context.bot.send_photo(photo=file, **kwargs, **c)
        try:
            return await _call_with_reply_fallback(_do, common)
        except BadRequest as exc:
            if not is_entity_error(exc):
                raise
            file.seek(0)
            clean = _strip_caption_kwargs(kwargs)
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_photo(photo=file, **clean, **c), common
            )


async def _send_audio(context: Any, path: Path, common: dict, caption: str | None, keyboard, title: str) -> Any:
    kwargs = {
        "caption": caption,
        "parse_mode": "HTML" if caption else None,
        "reply_markup": keyboard,
        "title": title[:64] if title else None,
    }

    if _local_api_enabled(context):
        try:
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_audio(audio=str(path), **kwargs, **c), common
            )
        except FloodLimitError:
            raise
        except Exception as local_exc:
            logger.warning("Local API send_audio failed, falling back: %s", local_exc)

    with path.open("rb") as file:
        async def _do(c):
            file.seek(0)
            return await context.bot.send_audio(audio=file, **kwargs, **c)
        try:
            return await _call_with_reply_fallback(_do, common)
        except BadRequest as exc:
            if not is_entity_error(exc):
                raise
            file.seek(0)
            clean = _strip_caption_kwargs(kwargs)
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_audio(audio=file, **clean, **c), common
            )


async def _send_document(context: Any, path: Path, common: dict, caption: str | None, keyboard) -> Any:
    kwargs = {
        "caption": caption,
        "parse_mode": "HTML" if caption else None,
        "reply_markup": keyboard,
    }

    if _local_api_enabled(context):
        try:
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_document(document=str(path), **kwargs, **c), common
            )
        except FloodLimitError:
            raise
        except Exception as local_exc:
            logger.warning("Local API send_document failed, falling back: %s", local_exc)

    with path.open("rb") as file:
        async def _do(c):
            file.seek(0)
            return await context.bot.send_document(document=file, filename=path.name, **kwargs, **c)
        try:
            return await _call_with_reply_fallback(_do, common)
        except BadRequest as exc:
            if not is_entity_error(exc):
                raise
            file.seek(0)
            clean = _strip_caption_kwargs(kwargs)
            return await _call_with_reply_fallback(
                lambda c: context.bot.send_document(document=file, filename=path.name, **clean, **c), common
            )


def _media_bucket(kind: str) -> str:
    # sendMediaGroup rule: photo and video can share an album, audio and
    # document can only be grouped with their own kind.
    return "visual" if kind in ("photo", "video") else kind


def _group_sendable(items: list[tuple[Path, str]]) -> list[list[tuple[Path, str]]]:
    """Splits consecutive same-bucket files into groups of MEDIA_GROUP_LIMIT."""
    groups: list[list[tuple[Path, str]]] = []
    current: list[tuple[Path, str]] = []
    current_bucket: str | None = None

    for item in items:
        bucket = _media_bucket(item[1])
        if current_bucket != bucket or len(current) >= MEDIA_GROUP_LIMIT:
            if current:
                groups.append(current)
            current = [item]
            current_bucket = bucket
        else:
            current.append(item)

    if current:
        groups.append(current)

    return groups


async def _send_media_group(
    context: Any,
    group: list[tuple[Path, str]],
    common: dict,
    title: str,
) -> list[Any]:
    """
    Sends several files as one album. Telegram albums do not support
    reply_markup, so the caller sends the caption/buttons separately.
    """
    local = _local_api_enabled(context)
    opened_files: list[Any] = []

    def _build_media_list(force_files: bool = False):
        media_list: list[Any] = []
        for path, kind in group:
            if local and not force_files:
                source: Any = str(path)
            else:
                source = path.open("rb")
                opened_files.append(source)

            if kind == "video":
                media_list.append(InputMediaVideo(media=source, supports_streaming=True))
            elif kind == "photo":
                media_list.append(InputMediaPhoto(media=source))
            elif kind == "audio":
                media_list.append(InputMediaAudio(media=source, title=(title[:64] if title else None)))
            else:
                media_list.append(InputMediaDocument(media=source))
        return media_list

    async def _do_send(media_list: list[Any], reply_to: int | None):
        try:
            return await _with_flood_retry(lambda: context.bot.send_media_group(
                media=media_list,
                chat_id=common["chat_id"],
                message_thread_id=common.get("message_thread_id"),
                reply_to_message_id=reply_to,
                write_timeout=600.0,
                read_timeout=600.0,
            ))
        except BadRequest as exc:
            if _is_reply_not_found(exc) and reply_to is not None:
                logger.warning("Reply target missing (album), retrying without a reply.")
                return await _with_flood_retry(lambda: context.bot.send_media_group(
                    media=media_list,
                    chat_id=common["chat_id"],
                    message_thread_id=common.get("message_thread_id"),
                    reply_to_message_id=None,
                    write_timeout=600.0,
                    read_timeout=600.0,
                ))
            raise

    try:
        try:
            return await _do_send(_build_media_list(), common.get("reply_to_message_id"))
        except FloodLimitError:
            raise
        except Exception as exc:
            if not local:
                raise
            # Local API album upload failed for a non-flood reason: reopen the
            # files and retry through the cloud API.
            logger.warning("Local API send_media_group failed, falling back: %s", exc)
            for f in opened_files:
                try:
                    f.close()
                except Exception:
                    pass
            opened_files.clear()

            return await _do_send(
                _build_media_list(force_files=True),
                common.get("reply_to_message_id"),
            )
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass


async def send_downloaded_files(
    *,
    context: Any,
    chat_id: int,
    thread_id: int | None,
    reply_to_message_id: int | None,
    files: list[str],
    title: str,
    source_url: str,
    info: dict | None = None,
    mode: str = "auto",
    user_id: int | None = None,
    bare: bool = False,
) -> list[dict]:
    """
    Sends the downloaded files. Multiple media are grouped into albums, both
    for a tidy result and to reduce the flood-control risk of many separate
    messages.

    bare=True (safe mode): no caption, keyboard or details menu; only the media
    is sent as a reply.

    Raises FloodLimitError when Telegram keeps refusing after every retry.

    Returns [{"file_id"?, "path", "kind"}] for the cache.
    """
    info = info or {}

    if not files:
        raise RuntimeError("No files to send.")

    caption = None if bare else final_caption(title or info.get("title") or "", source_url)
    has_description = bool(str(info.get("description") or "").strip())

    if bare:
        keyboard = None
    else:
        post_id = _create_media_post(
            context,
            info=info,
            source_url=source_url,
            title=title or info.get("title") or "",
            mode=mode,
            files=files,
            user_id=user_id,
        )
        keyboard = build_post_keyboard(post_id, source_url, has_description=has_description)

    max_bytes = _max_upload_bytes(context)
    sent_any = False
    skipped_any = False
    sent_items: list[dict] = []

    # 1) Pick the sendable files and warn about the ones over the limit.
    sendable: list[tuple[Path, str]] = []
    for file_path in files:
        path = Path(file_path)
        ext = path.suffix.lower()

        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0

        if file_size and file_size > max_bytes:
            skipped_any = True
            if not bare:
                await _send_too_large_message(
                    context=context,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id if not sent_any else None,
                    path=path,
                    file_size=file_size,
                    max_bytes=max_bytes,
                )
                sent_any = True  # only the first message carries the reply
            continue

        if ext in VIDEO_EXTS:
            kind = "video"
        elif ext in IMAGE_EXTS:
            # Telegram's "photo" type is capped at 10 MB. Larger images go as
            # documents so one big picture cannot fail the whole album
            # (sendMediaGroup is atomic).
            if file_size and file_size > PHOTO_MAX_BYTES:
                kind = "document"
                logger.info(
                    "%s exceeds the photo limit (%s > %s), sending as a document.",
                    path.name, human_bytes(file_size), human_bytes(PHOTO_MAX_BYTES),
                )
            else:
                kind = "photo"
        elif ext in AUDIO_EXTS:
            kind = "audio"
        else:
            kind = "document"
        sendable.append((path, kind))

    if not sendable:
        if skipped_any:
            return sent_items
        raise RuntimeError("No file could be sent.")

    # Stable sort by bucket (visual -> audio -> document) so a single oversized
    # image demoted to "document" does not split the photo/video album.
    _bucket_priority = {"visual": 0, "audio": 1, "document": 2}
    sendable.sort(key=lambda item: _bucket_priority[_media_bucket(item[1])])

    # A single file carries the caption and buttons directly; with several
    # files they are sent afterwards as one follow-up message, because
    # sendMediaGroup does not support reply_markup.
    single_file = len(sendable) == 1
    first_sent_message = None

    groups = _group_sendable(sendable)

    for group in groups:
        current_reply = reply_to_message_id if not sent_any else None

        if len(group) == 1:
            path, kind = group[0]
            attach_caption = caption if single_file else None
            attach_keyboard = keyboard if single_file else None
            common = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "reply_to_message_id": current_reply,
                "write_timeout": 600.0,
                "read_timeout": 600.0,
            }

            if kind == "video":
                sent_msg = await _send_video(context, path, common, attach_caption, attach_keyboard)
            elif kind == "photo":
                sent_msg = await _send_photo(context, path, common, attach_caption, attach_keyboard)
            elif kind == "audio":
                sent_msg = await _send_audio(
                    context, path, common, attach_caption, attach_keyboard,
                    title or info.get("title") or "",
                )
            else:
                sent_msg = await _send_document(context, path, common, attach_caption, attach_keyboard)

            if first_sent_message is None:
                first_sent_message = sent_msg
            sent_any = True
            sent_items.append({
                "file_id": _extract_file_id(sent_msg, kind),
                "path": str(path),
                "kind": kind,
            })
        else:
            common = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "reply_to_message_id": current_reply,
            }
            sent_msgs = await _send_media_group(
                context, group, common, title or info.get("title") or "",
            )
            if first_sent_message is None and sent_msgs:
                first_sent_message = sent_msgs[0]
            sent_any = True
            for (path, kind), msg in zip(group, sent_msgs):
                sent_items.append({
                    "file_id": _extract_file_id(msg, kind),
                    "path": str(path),
                    "kind": kind,
                })

    # With several files the caption and the details/source buttons go out as a
    # single follow-up message.
    if not bare and not single_file and (caption or keyboard):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                reply_to_message_id=getattr(first_sent_message, "message_id", None),
                text=caption or final_caption(title or info.get("title") or "", source_url),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            logger.warning("Could not send the details/source follow-up.", exc_info=True)

    if not sent_any and skipped_any:
        return sent_items

    if not sent_any:
        raise RuntimeError("No file could be sent.")

    return sent_items


async def send_from_cache(
    *,
    context: Any,
    chat_id: int,
    thread_id: int | None,
    reply_to_message_id: int | None,
    record: dict,
    source_url: str,
    bare: bool = False,
) -> list[dict]:
    """
    Sends a cached record: by file_id when available, otherwise by re-uploading
    the file from disk (and refreshing the file_id).

    Returns the updated [{"file_id", "path", "kind"}] list.
    """
    items = record.get("items", [])
    title = record.get("title", "")
    info = record.get("info", {})

    caption = None if bare else final_caption(title or info.get("title") or "", source_url)
    has_description = bool(str(info.get("description") or "").strip())

    if bare:
        keyboard = None
    else:
        post_id = _create_media_post(
            context,
            info=info,
            source_url=source_url,
            title=title,
            mode=record.get("mode", "auto"),
            files=[it.get("path") for it in items if it.get("path")],
            user_id=None,
        )
        keyboard = build_post_keyboard(post_id, source_url, has_description=has_description)

    result_items: list[dict] = []
    sent_any = False

    for item in items:
        kind = item.get("kind", "document")
        file_id = item.get("file_id")
        path_str = item.get("path")

        current_caption = caption if not sent_any else None
        current_keyboard = keyboard if not sent_any else None
        common = {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "reply_to_message_id": reply_to_message_id if not sent_any else None,
            "write_timeout": 600.0,
            "read_timeout": 600.0,
        }

        sent_msg = None
        if file_id:
            try:
                sent_msg = await _send_by_file_id(context, kind, file_id, common, current_caption, current_keyboard)
            except Exception as exc:
                logger.warning("Cached file_id send failed, falling back to disk: %s", exc)
                sent_msg = None

        if sent_msg is None and path_str and Path(path_str).is_file():
            path = Path(path_str)
            ext = path.suffix.lower()
            if ext in VIDEO_EXTS:
                kind = "video"
                sent_msg = await _send_video(context, path, common, current_caption, current_keyboard)
            elif ext in IMAGE_EXTS:
                kind = "photo"
                sent_msg = await _send_photo(context, path, common, current_caption, current_keyboard)
            elif ext in AUDIO_EXTS:
                kind = "audio"
                sent_msg = await _send_audio(context, path, common, current_caption, current_keyboard, title)
            else:
                kind = "document"
                sent_msg = await _send_document(context, path, common, current_caption, current_keyboard)

        if sent_msg is None:
            continue

        sent_any = True
        result_items.append({
            "file_id": _extract_file_id(sent_msg, kind) or file_id,
            "path": path_str,
            "kind": kind,
        })

    if not sent_any:
        raise RuntimeError("Nothing sendable left in the cache record.")

    return result_items


async def _send_by_file_id(context: Any, kind: str, file_id: str, common: dict, caption: str | None, keyboard):
    kwargs = {
        "caption": caption,
        "parse_mode": "HTML" if caption else None,
        "reply_markup": keyboard,
    }
    if kind == "video":
        return await _call_with_reply_fallback(
            lambda c: context.bot.send_video(video=file_id, supports_streaming=True, **kwargs, **c), common
        )
    if kind == "photo":
        return await _call_with_reply_fallback(
            lambda c: context.bot.send_photo(photo=file_id, **kwargs, **c), common
        )
    if kind == "audio":
        return await _call_with_reply_fallback(
            lambda c: context.bot.send_audio(audio=file_id, **kwargs, **c), common
        )
    return await _call_with_reply_fallback(
        lambda c: context.bot.send_document(document=file_id, **kwargs, **c), common
    )
