from __future__ import annotations

"""
Usage analytics.

1) ActivityBuffer — a write buffer. Writing "I saw this user" on every message
   is wasteful I/O, so touches are collected in memory and flushed in a single
   transaction. Download records are not buffered; they are rare and valuable.

2) Queries — the summaries the admin dashboard needs, aggregated in SQL.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("downloader")

FLUSH_INTERVAL = 30.0     # seconds
FLUSH_THRESHOLD = 50      # flush immediately once this many touches pile up


@dataclass
class _Touch:
    username: str | None = None
    first_name: str | None = None
    language: str | None = None
    title: str | None = None
    chat_type: str | None = None
    last_seen: float = field(default_factory=time.time)


class ActivityBuffer:
    """
    Collects user/chat activity updates and writes them in batches.

    The risk is deliberate: a crash loses at most FLUSH_INTERVAL seconds of
    "last seen" updates, which is not critical data.
    """

    def __init__(self, db: Any):
        self.db = db
        self._users: dict[int, _Touch] = {}
        self._chats: dict[int, _Touch] = {}
        self._lock = asyncio.Lock()
        self._last_flush = time.time()

    def pending(self) -> int:
        return len(self._users) + len(self._chats)

    async def touch_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        language: str | None = None,
    ) -> None:
        async with self._lock:
            entry = self._users.setdefault(user_id, _Touch())
            entry.username = username or entry.username
            entry.first_name = first_name or entry.first_name
            entry.language = language or entry.language
            entry.last_seen = time.time()
        await self._maybe_flush()

    async def touch_chat(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        chat_type: str | None = None,
    ) -> None:
        async with self._lock:
            entry = self._chats.setdefault(chat_id, _Touch())
            entry.title = title or entry.title
            entry.chat_type = chat_type or entry.chat_type
            entry.last_seen = time.time()
        await self._maybe_flush()

    async def _maybe_flush(self) -> None:
        if self.pending() >= FLUSH_THRESHOLD:
            await self.flush()

    async def flush(self) -> int:
        """Writes pending touches in one go. Returns the number of rows."""
        async with self._lock:
            users, chats = self._users, self._chats
            self._users, self._chats = {}, {}

        if not users and not chats:
            self._last_flush = time.time()
            return 0

        def _write() -> int:
            written = 0
            for user_id, entry in users.items():
                try:
                    self.db.touch_user(
                        user_id,
                        username=entry.username,
                        first_name=entry.first_name,
                        language=entry.language,
                    )
                    written += 1
                except Exception:
                    logger.exception("Activity write failed (user=%s)", user_id)
            for chat_id, entry in chats.items():
                try:
                    self.db.touch_chat(
                        chat_id, title=entry.title, chat_type=entry.chat_type
                    )
                    written += 1
                except Exception:
                    logger.exception("Activity write failed (chat=%s)", chat_id)
            return written

        written = await asyncio.to_thread(_write)
        self._last_flush = time.time()
        return written


async def activity_flusher(buffer: ActivityBuffer, interval: float = FLUSH_INTERVAL) -> None:
    """Background task that flushes the buffer periodically."""
    while True:
        try:
            await asyncio.sleep(interval)
            written = await buffer.flush()
            if written:
                logger.debug("Activity buffer flushed: %d rows", written)
        except asyncio.CancelledError:
            try:
                await buffer.flush()
            except Exception:
                pass
            raise
        except Exception:
            logger.exception("Activity buffer flush failed")


# ── Queries ──────────────────────────────────────────────────────────────────

def active_users(db: Any, days: int) -> int:
    cutoff = time.time() - days * 86400
    row = db.query_one(
        f"SELECT COUNT(*) AS c FROM users WHERE last_activity >= {db.ph}", (cutoff,)
    )
    return int((row or {}).get("c", 0))


def downloads_since(db: Any, days: int) -> int:
    cutoff = time.time() - days * 86400
    row = db.query_one(
        f"SELECT COUNT(*) AS c FROM downloads "
        f"WHERE created_at >= {db.ph} AND result = 'success'",
        (cutoff,),
    )
    return int((row or {}).get("c", 0))


def daily_counts(db: Any, days: int = 7) -> list[dict[str, Any]]:
    """
    Daily download counts for the last N days, oldest first.

    day_offset counts backwards from today: 0 is today, 1 is yesterday, and so
    on, so the returned list can be charted left to right as time.
    """
    cutoff = time.time() - days * 86400
    rows = db.query(
        f"""SELECT CAST((created_at - {db.ph}) / 86400 AS INTEGER) AS bucket,
                   COUNT(*) AS count
            FROM downloads
            WHERE created_at >= {db.ph} AND result = 'success'
            GROUP BY bucket ORDER BY bucket""",
        (cutoff, cutoff),
    )
    # bucket 0 is the oldest day in the window, bucket days-1 is today.
    counts = {int(r["bucket"]): int(r["count"]) for r in rows}
    return [
        {"day_offset": days - 1 - bucket, "count": counts.get(bucket, 0)}
        for bucket in range(days)
    ]


def top_users(db: Any, limit: int = 10) -> list[dict[str, Any]]:
    return db.query(
        f"""SELECT user_id, username, first_name, total_downloads, last_activity
            FROM users WHERE total_downloads > 0
            ORDER BY total_downloads DESC LIMIT {db.ph}""",
        (limit,),
    )


def platform_distribution(db: Any, days: int | None = None) -> list[dict[str, Any]]:
    if days:
        cutoff = time.time() - days * 86400
        return db.query(
            f"""SELECT platform, COUNT(*) AS count FROM downloads
                WHERE result='success' AND platform <> '' AND created_at >= {db.ph}
                GROUP BY platform ORDER BY count DESC""",
            (cutoff,),
        )
    return db.query(
        """SELECT platform, COUNT(*) AS count FROM downloads
           WHERE result='success' AND platform <> ''
           GROUP BY platform ORDER BY count DESC"""
    )


def source_distribution(db: Any) -> list[dict[str, Any]]:
    """How much work each download source (cobalt/ytdlp/gallerydl) did."""
    return db.query(
        """SELECT source, COUNT(*) AS count FROM downloads
           WHERE result='success' AND source <> ''
           GROUP BY source ORDER BY count DESC"""
    )


def chat_type_split(db: Any) -> dict[str, int]:
    rows = db.query(
        """SELECT chat_type, COUNT(*) AS count FROM chats
           WHERE chat_type <> '' GROUP BY chat_type"""
    )
    out = {"private": 0, "group": 0}
    for row in rows:
        kind = str(row["chat_type"])
        if kind == "private":
            out["private"] += int(row["count"])
        elif kind in {"group", "supergroup"}:
            out["group"] += int(row["count"])
    return out


def failure_rate(db: Any, days: int = 7) -> dict[str, Any]:
    cutoff = time.time() - days * 86400
    row = db.query_one(
        f"""SELECT
              SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS ok,
              COUNT(*) AS total
            FROM downloads WHERE created_at >= {db.ph}""",
        (cutoff,),
    ) or {}
    ok = int(row.get("ok") or 0)
    total = int(row.get("total") or 0)
    return {
        "ok": ok,
        "total": total,
        "failed": total - ok,
        "rate": (ok * 100.0 / total) if total else 0.0,
    }


def summary(db: Any) -> dict[str, Any]:
    """Everything the dashboard needs in a single call."""
    base = db.stats()
    return {
        **base,
        "dau": active_users(db, 1),
        "wau": active_users(db, 7),
        "mau": active_users(db, 30),
        "downloads_today": downloads_since(db, 1),
        "downloads_week": downloads_since(db, 7),
        "chat_split": chat_type_split(db),
        "failure": failure_rate(db, 7),
    }
