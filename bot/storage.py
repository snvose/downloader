from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(tmp_path, path)


def ensure_json_file(path: Path, default: Any) -> None:
    if not path.exists():
        write_json_atomic(path, default)


def init_runtime_files(data_dir: Path) -> None:
    ensure_json_file(data_dir / "banned_users.json", {"users": [], "groups": []})
    # bot_state kalıcı "mode" alanı tutar (normal|safe|maintenance)
    ensure_json_file(data_dir / "bot_state.json", {"enabled": True, "mode": "normal"})
    ensure_json_file(data_dir / "usage_stats.json", {
        "total_users": 0,
        "total_groups": 0,
        "total_downloads": 0,
        "failed_downloads": 0,
        "cancelled_downloads": 0,
        "platforms": {},
        "users": {},
        "groups": {},
    })
    ensure_json_file(data_dir / "temp_bans.json", {"users": {}})
    # Kalıcı veri dosyaları
    ensure_json_file(data_dir / "cache.json", {})
    ensure_json_file(data_dir / "chats.json", {"chats": {}})

    if not (data_dir / "emoji_slots.json").exists():
        write_json_atomic(data_dir / "emoji_slots.json", {})


def increment_stat(data_dir: Path, key: str, platform: str | None = None) -> None:
    stats_file = data_dir / "usage_stats.json"
    data = read_json(stats_file, {})

    if not isinstance(data, dict):
        data = {}

    try:
        data[key] = int(data.get(key, 0)) + 1
    except Exception:
        data[key] = 1

    if platform and key == "total_downloads":
        platforms = data.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            platforms = {}
            data["platforms"] = platforms
        platforms[platform] = int(platforms.get(platform, 0)) + 1

    write_json_atomic(stats_file, data)
