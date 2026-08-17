from __future__ import annotations

"""
Chat usage registry.

Every chat the bot is used in is stored in data/chats.json: chat id, title,
type, total downloads and last activity. Updated on each successful download.
"""

import time
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic


class ChatRegistry:
    def __init__(self, data_dir: Path):
        self.chats_file = data_dir / "chats.json"

    def _load(self) -> dict:
        data = read_json(self.chats_file, {"chats": {}})
        if not isinstance(data, dict) or not isinstance(data.get("chats"), dict):
            data = {"chats": {}}
        return data

    def _save(self, data: dict) -> None:
        write_json_atomic(self.chats_file, data)

    def record_download(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        platform: str = "",
    ) -> None:
        data = self._load()
        chats = data["chats"]
        key = str(chat_id)

        entry = chats.get(key) or {
            "chat_id": chat_id,
            "title": title,
            "type": chat_type,
            "total_downloads": 0,
            "platforms": {},
            "first_seen": time.time(),
        }

        entry["title"] = title or entry.get("title", "")
        entry["type"] = chat_type or entry.get("type", "")
        entry["total_downloads"] = int(entry.get("total_downloads", 0)) + 1
        entry["last_activity"] = time.time()

        if platform:
            plats = entry.setdefault("platforms", {})
            plats[platform] = int(plats.get(platform, 0)) + 1

        chats[key] = entry
        self._save(data)

    def all_chats(self) -> list[dict]:
        """All chats, most recently active first."""
        chats = list(self._load()["chats"].values())
        chats.sort(key=lambda c: c.get("last_activity", 0), reverse=True)
        return chats

    def stats(self) -> dict[str, Any]:
        chats = self.all_chats()
        groups = [c for c in chats if c.get("type") in {"group", "supergroup"}]
        privates = [c for c in chats if c.get("type") == "private"]
        return {
            "total_chats": len(chats),
            "groups": len(groups),
            "privates": len(privates),
            "total_downloads": sum(int(c.get("total_downloads", 0)) for c in chats),
        }
