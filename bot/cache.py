from __future__ import annotations

"""
Persistent cache that avoids re-downloading the same link.

  1. file_id known        -> forward directly, no disk access
  2. file missing file_id -> re-upload from disk and store the new file_id
  3. neither              -> run the normal download

Stored in data/cache.json, keyed by (normalised URL + mode). A record may hold
several files (media + cover); each file keeps both a file_id and a disk path.

The I/O here is blocking, so handlers must call it through asyncio.to_thread.
"""

import hashlib
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .storage import read_json, write_json_atomic


# Query parameters that identify the content and must stay in the cache key.
# Dropping v= or list= would make different videos collide on one key.
_SIGNIFICANT_PARAMS = {"v", "list", "index"}

# Upper bound on stored records. The whole file is rewritten on every store,
# so an unbounded cache slowly turns every download into a large disk write.
MAX_RECORDS = 5000


def _normalize_for_key(url: str) -> str:
    """host + path + significant query params, tracking parameters dropped."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parts.path or "").rstrip("/")

        kept = sorted(
            (k.lower(), v)
            for k, v in parse_qsl(parts.query)
            if k.lower() in _SIGNIFICANT_PARAMS
        )
        query = "&".join(f"{k}={v}" for k, v in kept)
        base = f"{host}{path}"
        if query:
            base += "?" + query
        return base.lower()
    except Exception:
        return (url or "").strip().lower().split("?", 1)[0].rstrip("/")


def make_key(url: str, mode: str = "auto") -> str:
    raw = f"{_normalize_for_key(url)}::{mode or 'auto'}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class MediaCache:
    def __init__(self, data_dir: Path, *, enabled: bool = True, max_records: int = MAX_RECORDS):
        self.cache_file = data_dir / "cache.json"
        self.enabled = enabled
        self.max_records = int(max_records)

    def _load(self) -> dict:
        data = read_json(self.cache_file, {})
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        write_json_atomic(self.cache_file, data)

    def _trim(self, data: dict) -> dict:
        """Keeps the newest max_records entries."""
        if self.max_records <= 0 or len(data) <= self.max_records:
            return data
        ordered = sorted(
            data.items(),
            key=lambda item: float((item[1] or {}).get("updated_at") or 0.0),
            reverse=True,
        )
        return dict(ordered[: self.max_records])

    def get(self, url: str, mode: str = "auto") -> dict | None:
        if not self.enabled:
            return None
        return self._load().get(make_key(url, mode))

    def resolve_sendable(self, url: str, mode: str = "auto") -> dict | None:
        """
        Returns a record that can actually be sent, i.e. at least one item has
        a file_id or an existing file on disk. Otherwise None, which triggers
        a normal download.
        """
        record = self.get(url, mode)
        if not record:
            return None

        usable: list[dict] = []
        for item in record.get("items", []):
            file_id = item.get("file_id")
            path = item.get("path")
            if file_id:
                usable.append(item)
            else:
                try:
                    if path and Path(path).is_file() and Path(path).stat().st_size > 0:
                        usable.append(item)
                except OSError:
                    pass

        if not usable:
            return None

        out = dict(record)
        out["items"] = usable
        return out

    def store(
        self,
        url: str,
        mode: str,
        items: list[dict],
        *,
        title: str = "",
        info: dict | None = None,
    ) -> None:
        """items: [{"file_id": str|None, "path": str|None, "kind": str}]"""
        if not self.enabled:
            return
        clean_items = [it for it in items if it.get("file_id") or it.get("path")]
        if not clean_items:
            return

        data = self._load()
        data[make_key(url, mode)] = {
            "url": url,
            "mode": mode,
            "title": title,
            "info": info or {},
            "items": clean_items,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(self._trim(data))

    def update_file_ids(self, url: str, mode: str, items: list[dict]) -> None:
        """Writes fresh file_ids onto an existing record."""
        if not self.enabled:
            return
        data = self._load()
        key = make_key(url, mode)
        record = data.get(key)
        if not record:
            self.store(url, mode, items)
            return
        record["items"] = [it for it in items if it.get("file_id") or it.get("path")]
        record["updated_at"] = time.time()
        data[key] = record
        self._save(self._trim(data))

    def prune_missing_files(self) -> int:
        """
        Drops items whose file is gone and that have no file_id either.
        Called by the daily cleanup. Returns the number of removed records.
        """
        data = self._load()
        removed = 0
        new_data: dict = {}

        for key, record in data.items():
            items = record.get("items", [])
            kept: list[dict] = []
            for item in items:
                file_id = item.get("file_id")
                path = item.get("path")
                if file_id:
                    # Still sendable by file_id; just clear the dead disk path.
                    if path and not Path(path).exists():
                        item = dict(item)
                        item["path"] = None
                    kept.append(item)
                elif path and Path(path).exists():
                    kept.append(item)
            if kept:
                record = dict(record)
                record["items"] = kept
                new_data[key] = record
            else:
                removed += 1

        if removed:
            self._save(new_data)
        return removed
