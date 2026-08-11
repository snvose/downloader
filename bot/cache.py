from __future__ import annotations

"""
bot/cache.py — Aynı linkin tekrar indirilmesini önleyen kalıcı cache.

Akış:
  1. file_id varsa  -> diske gitmeden direkt file_id ile ilet.
  2. file_id yok ama dosya diskte varsa -> yeniden yükle, file_id'yi kaydet.
  3. Ne file_id ne dosya varsa -> normal indirme sürecini başlat.

Veri data/cache.json içinde KALICI saklanır. Anahtar: (normalize URL + mode).
Her kayıt birden çok dosya tutabilir (medya + kapak gibi). Her dosya için
hem file_id hem disk yolu saklanır; biri yoksa diğerine düşülür.

Senkron dosya I/O içerdiği için handler'lardan asyncio.to_thread() ile
çağrılmalıdır (bkz. app.py / links.py).
"""

import hashlib
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .storage import read_json, write_json_atomic


# İçeriği TANIMLAYAN query parametreleri (cache anahtarında korunur).
# YouTube/YT Music: v= (video), list= (playlist). Bunlar atılırsa farklı
# videolar aynı anahtara düşer ve yanlış medya servis edilir.
_SIGNIFICANT_PARAMS = {"v", "list", "index"}


def _normalize_for_key(url: str) -> str:
    """URL'yi içerik kimliğini koruyacak, takip parametrelerini atacak biçimde
    normalize eder. (host + path + yalnızca anlamlı query parametreleri)"""
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
    def __init__(self, data_dir: Path, *, enabled: bool = True):
        self.cache_file = data_dir / "cache.json"
        self.enabled = enabled

    def _load(self) -> dict:
        data = read_json(self.cache_file, {})
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        write_json_atomic(self.cache_file, data)

    def get(self, url: str, mode: str = "auto") -> dict | None:
        """Cache kaydını döner. enabled=False ise her zaman None."""
        if not self.enabled:
            return None
        return self._load().get(make_key(url, mode))

    def resolve_sendable(self, url: str, mode: str = "auto") -> dict | None:
        """
        Gönderilebilir kayıt döner:
          {"items": [{"file_id"?: str, "path"?: str, "kind": str}], "title", "info"...}
        En az bir öğenin file_id'si VEYA diskte mevcut dosyası olmalı; aksi halde
        None döner ve normal indirme tetiklenir.
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
            elif path and Path(path).is_file() and Path(path).stat().st_size > 0:
                usable.append(item)

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
        """
        items: [{"file_id": str|None, "path": str|None, "kind": str}]
        En az bir kullanılabilir öğe yoksa kayıt yapılmaz.
        """
        if not self.enabled:
            return
        clean_items = [
            it for it in items
            if it.get("file_id") or it.get("path")
        ]
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
        self._save(data)

    def update_file_ids(self, url: str, mode: str, items: list[dict]) -> None:
        """Var olan kayda yeni file_id'leri işler (disk→yükle→file_id senaryosu)."""
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
        self._save(data)

    def prune_missing_files(self) -> int:
        """
        Diskteki dosyası silinmiş VE file_id'si de olmayan öğeleri/kayıtları
        temizler. Günlük temizlik görevinden çağrılır. Temizlenen kayıt sayısını
        döner.
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
                    # file_id ile gönderim hâlâ mümkün; sadece ölü disk yolunu boşalt
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
