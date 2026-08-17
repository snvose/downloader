from __future__ import annotations

"""
One-time import of the legacy JSON data into SQLite.

data/chats.json and data/usage_stats.json are read once, on the first start
with a database. The JSON files are NOT deleted (so a rollback stays possible)
and the import is marked in schema_meta so it never repeats.
"""

import logging
import time
from pathlib import Path

from .db import Database
from .storage import read_json

logger = logging.getLogger("downloader")


def _already_migrated(db: Database) -> bool:
    row = db.query_one(
        f"SELECT value FROM schema_meta WHERE key = {db.ph}", ("json_import",)
    )
    return bool(row and row.get("value"))


def _mark_migrated(db: Database, summary: str) -> None:
    db.execute(
        f"INSERT OR REPLACE INTO schema_meta (key, value) VALUES ({db.ph}, {db.ph})",
        ("json_import", summary),
    )


def migrate_json_to_db(db: Database, data_dir: Path) -> dict[str, int]:
    """
    chats.json + usage_stats.json -> SQLite.

    Returns {"chats": n, "platforms": n, "users": n}; zeros when already done.
    """
    if _already_migrated(db):
        return {"chats": 0, "platforms": 0, "users": 0, "skipped": 1}

    data_dir = Path(data_dir)
    now = time.time()
    counts = {"chats": 0, "platforms": 0, "users": 0, "skipped": 0}

    # ── chats.json ──
    chats_data = read_json(data_dir / "chats.json", {"chats": {}})
    chats = chats_data.get("chats") if isinstance(chats_data, dict) else {}

    if isinstance(chats, dict):
        for key, entry in chats.items():
            if not isinstance(entry, dict):
                continue
            try:
                chat_id = int(entry.get("chat_id") or key)
            except (TypeError, ValueError):
                continue

            first_seen = float(entry.get("first_seen") or now)
            last_activity = float(entry.get("last_activity") or first_seen)
            total = int(entry.get("total_downloads") or 0)
            title = str(entry.get("title") or "")
            chat_type = str(entry.get("type") or entry.get("chat_type") or "")

            db.execute(
                f"""INSERT OR REPLACE INTO chats
                       (chat_id, title, chat_type, first_seen, last_activity,
                        total_downloads, broadcast_opt_out, is_blocked)
                    VALUES ({db.ph}, {db.ph}, {db.ph}, {db.ph}, {db.ph}, {db.ph}, 0, 0)""",
                (chat_id, title, chat_type, first_seen, last_activity, total),
            )
            counts["chats"] += 1

            platforms = entry.get("platforms")
            if isinstance(platforms, dict):
                for platform, count in platforms.items():
                    try:
                        count = int(count)
                    except (TypeError, ValueError):
                        continue
                    if count <= 0:
                        continue
                    db.execute(
                        f"""INSERT OR REPLACE INTO chat_platforms (chat_id, platform, count)
                            VALUES ({db.ph}, {db.ph}, {db.ph})""",
                        (chat_id, str(platform), count),
                    )
                    counts["platforms"] += 1

            # In private chats chat_id == user_id, so the user row is created
            # as well and the broadcast target list is populated from day one.
            if chat_type == "private" and chat_id > 0:
                db.execute(
                    f"""INSERT OR REPLACE INTO users
                           (user_id, username, first_name, language, first_seen,
                            last_activity, total_downloads, broadcast_opt_out, is_blocked)
                        VALUES ({db.ph}, NULL, NULL, NULL, {db.ph}, {db.ph}, {db.ph}, 0, 0)""",
                    (chat_id, first_seen, last_activity, total),
                )
                counts["users"] += 1

    # ── usage_stats.json: user ids ──
    stats = read_json(data_dir / "usage_stats.json", {})
    if isinstance(stats, dict):
        users = stats.get("users")
        if isinstance(users, dict):
            for key, value in users.items():
                try:
                    user_id = int(key)
                except (TypeError, ValueError):
                    continue
                if db.get_user(user_id):
                    continue
                total = int(value) if isinstance(value, (int, float)) else 0
                db.execute(
                    f"""INSERT INTO users
                           (user_id, username, first_name, language, first_seen,
                            last_activity, total_downloads, broadcast_opt_out, is_blocked)
                        VALUES ({db.ph}, NULL, NULL, NULL, {db.ph}, {db.ph}, {db.ph}, 0, 0)""",
                    (user_id, now, now, total),
                )
                counts["users"] += 1

    summary = f"{counts['chats']} chats, {counts['users']} users @ {int(now)}"
    _mark_migrated(db, summary)
    logger.info("JSON -> SQLite import finished: %s", summary)

    return counts
