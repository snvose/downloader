from __future__ import annotations

"""
Bulk announcement delivery.

  • RATE LIMIT: Telegram allows roughly 30 messages per second across different
    chats. The default here is 20/s. If Telegram still answers with RetryAfter,
    the requested delay is honoured and that target is retried once.

  • PERMANENTLY UNREACHABLE targets (blocked the bot, deleted account, kicked)
    are marked with is_blocked=1 in the database and skipped by the next
    broadcast, instead of wasting dozens of requests every time.

  • TEMPORARY failures (timeout, bad gateway) are only counted, never marked.

  • CANCELLABLE: a long running broadcast can be stopped by the admin.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("downloader")

MESSAGES_PER_SECOND = 20
SEND_DELAY = 1.0 / MESSAGES_PER_SECOND

# These phrases mean the target is permanently unreachable.
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
    """State of a single broadcast."""

    text: str
    targets: list[int]
    kind: str = "all"                 # all | users | groups
    parse_mode: str | None = "HTML"
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    sent: int = 0
    failed: int = 0
    blocked: int = 0                  # permanently unreachable, marked in the db
    cancelled: bool = False
    running: bool = False

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

        state = "🛑 Cancelled" if self.cancelled else (
            "📤 Sending" if self.running else "✅ Finished"
        )

        return (
            f"<b>{state}</b>\n\n"
            f"<code>[{bar}]</code> <b>{percent}%</b>\n"
            f"Processed: <b>{done}</b> / {self.total}\n"
            f"✅ Delivered: <b>{self.sent}</b>\n"
            f"🚫 Unreachable: <b>{self.blocked}</b>\n"
            f"⚠️ Errors: <b>{self.failed}</b>\n"
            f"⏱ Elapsed: <b>{self.duration:.0f} s</b>"
        )

    def summary_text(self) -> str:
        lines = [
            "🛑 <b>Broadcast cancelled</b>" if self.cancelled
            else "✅ <b>Broadcast finished</b>",
            "",
            f"📊 Targets: <b>{self.total}</b>",
            f"✅ Delivered: <b>{self.sent}</b>",
            f"🚫 Blocked / unreachable: <b>{self.blocked}</b>",
            f"⚠️ Temporary errors: <b>{self.failed}</b>",
            f"⏱ Elapsed: <b>{self.duration:.0f} seconds</b>",
        ]

        if self.total:
            rate = self.sent * 100 / self.total
            lines.append(f"📈 Success rate: <b>{rate:.0f}%</b>")

        if self.blocked:
            lines.append(
                f"\n<i>{self.blocked} records were marked and will be skipped "
                "by the next broadcast.</i>"
            )

        if self.errors:
            sample = "\n".join(f"• {e}" for e in self.errors[:3])
            lines.append(f"\n<b>Sample errors</b>\n{sample}")

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
    Sends the broadcast, queued and rate limited.

    on_progress: async callback invoked every `progress_every` targets.
    """
    job.running = True
    job.started_at = time.time()

    async def _send(chat_id: int) -> None:
        await app.bot.send_message(
            chat_id=chat_id,
            text=job.text,
            parse_mode=job.parse_mode,
            disable_web_page_preview=True,
        )

    for index, chat_id in enumerate(job.targets, start=1):
        if job.cancelled:
            break

        try:
            await _send(chat_id)
            job.sent += 1

        except Exception as exc:
            # If Telegram says "too fast", wait as long as it asks and retry.
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                logger.warning("Broadcast flood limit: waiting %s s", retry_after)
                await asyncio.sleep(float(retry_after) + 1.0)
                try:
                    await _send(chat_id)
                    job.sent += 1
                    exc = None
                except Exception as retry_exc:
                    exc = retry_exc

            if exc is not None:
                if _is_permanent_failure(exc):
                    job.blocked += 1
                    if db:
                        try:
                            # In a private chat chat_id == user_id.
                            await asyncio.to_thread(
                                db.mark_blocked,
                                user_id=chat_id if chat_id > 0 else None,
                                chat_id=chat_id if chat_id < 0 else None,
                            )
                        except Exception:
                            logger.exception("Could not mark blocked target: %s", chat_id)
                else:
                    job.failed += 1
                    if len(job.errors) < 5:
                        job.errors.append(f"{chat_id}: {str(exc)[:90]}")

        await asyncio.sleep(SEND_DELAY)

        if on_progress and index % progress_every == 0:
            try:
                await on_progress(job)
            except Exception:
                logger.exception("Broadcast progress update failed")

    job.running = False
    job.finished_at = time.time()

    logger.info(
        "BROADCAST finished | targets=%d delivered=%d blocked=%d errors=%d duration=%.0fs",
        job.total, job.sent, job.blocked, job.failed, job.duration,
    )

    if on_progress:
        try:
            await on_progress(job)
        except Exception:
            pass

    return job
