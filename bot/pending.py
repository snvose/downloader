from __future__ import annotations

"""
Lifecycle of the YouTube format selection menu.

Deleting the menu message happens in exactly one place (clear_pending_job) and
every path uses it: selection, a new link, cancel, error, admin reset and
expiry.
"""

import logging
import time
from typing import Any

logger = logging.getLogger("downloader")

# A menu untouched for this long is removed.
PENDING_TTL_SECONDS = 30 * 60


async def delete_menu_message(app: Any, job: dict[str, Any]) -> None:
    """
    Deletes the menu message of a pending job. If deletion fails (already
    gone, too old, missing rights) the buttons are removed instead.
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
    jobs = app.bot_data.get("pending_jobs") or {}
    job = jobs.pop(job_id, None)

    if job and delete_message:
        await delete_menu_message(app, job)

    return job


async def clear_user_pending(app: Any, user_id: int) -> dict | None:
    """Clears the user's pending menu (called when a new link arrives)."""
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
    """Clears every pending menu (admin reset)."""
    jobs = app.bot_data.get("pending_jobs") or {}
    job_ids = list(jobs.keys())

    for job_id in job_ids:
        try:
            await clear_pending_job(app, job_id)
        except Exception:
            jobs.pop(job_id, None)

    return len(job_ids)


async def expire_pending_jobs(app: Any, *, ttl: float = PENDING_TTL_SECONDS) -> int:
    """Removes menus that timed out, so they neither pile up nor confuse users."""
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
        logger.info("Removed %d expired format menus.", len(expired))

    return len(expired)
