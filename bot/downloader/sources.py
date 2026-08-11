from __future__ import annotations

"""
bot/downloader/sources.py — indirme kaynağı öncelik yönetimi.

Bot üç kaynaktan indirebilir:
    cobalt    — kendi barındırdığın cobalt instance'ı (hızlı, sunucu taraflı)
    ytdlp     — yt-dlp (en geniş platform desteği)
    gallerydl — gallery-dl (galeri/çoklu görsel içerikte güçlü)

Sıra SABİT KODLANMAZ; data/sources.json'dan okunur ve platform bazında
değiştirilebilir. Dosya yoksa varsayılan sıra kullanılır ve dosya oluşturulur.

Örnek data/sources.json:
    {
      "default": ["ytdlp", "cobalt", "gallerydl"],
      "platforms": {
        "TikTok":    ["cobalt", "ytdlp"],
        "Instagram": ["cobalt", "ytdlp", "gallerydl"],
        "YouTube":   ["ytdlp"]
      }
    }
"""

import logging
from pathlib import Path
from typing import Any

from bot.storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

KNOWN_SOURCES = ("cobalt", "ytdlp", "gallerydl")

# ── Varsayılan öncelikler ────────────────────────────────────────────────────
# Gerekçe:
#   TikTok/Instagram/Twitter/Reddit/Pinterest → cobalt önce: bu platformlar
#     sık HTML/imza değişikliği yapar; cobalt sunucu tarafında güncel kalır ve
#     watermark'sız / çoklu medya (picker) sonuçlarını tek istekte verir.
#   YouTube/YouTube Music → yt-dlp önce: format seçimi, altyazı, playlist ve
#     ses kalitesi kontrolü yt-dlp'de çok daha ayrıntılı; bot menüsü buna dayalı.
#   Diğer her şey → yt-dlp önce (en geniş kapsam), sonra cobalt.
DEFAULT_PRIORITY: dict[str, Any] = {
    "default": ["ytdlp", "cobalt", "gallerydl"],
    "platforms": {
        "TikTok": ["cobalt", "ytdlp", "gallerydl"],
        "Instagram": ["cobalt", "ytdlp", "gallerydl"],
        "X/Twitter": ["cobalt", "ytdlp", "gallerydl"],
        "Reddit": ["cobalt", "ytdlp", "gallerydl"],
        "Pinterest": ["cobalt", "ytdlp", "gallerydl"],
        "Facebook": ["cobalt", "ytdlp", "gallerydl"],
        "YouTube": ["ytdlp", "cobalt"],
        "YouTube Music": ["ytdlp", "cobalt"],
        "SoundCloud": ["ytdlp", "cobalt"],
        "Spotify": ["ytdlp"],
    },
}


def _sanitize(order: Any) -> list[str]:
    """Bilinmeyen/yinelenen kaynak adlarını ayıklar."""
    if not isinstance(order, list):
        return []
    clean: list[str] = []
    for item in order:
        name = str(item).strip().lower()
        if name in KNOWN_SOURCES and name not in clean:
            clean.append(name)
    return clean


class SourcePriority:
    """data/sources.json'u okur ve platform → kaynak sırası çözer."""

    def __init__(self, data_dir: Path):
        self.file = Path(data_dir) / "sources.json"
        self._cache: dict[str, Any] | None = None
        self._mtime: float = 0.0

    def ensure_file(self) -> None:
        if not self.file.exists():
            write_json_atomic(self.file, DEFAULT_PRIORITY)

    def _load(self) -> dict[str, Any]:
        # Dosya elle düzenlenebilsin diye mtime'a göre yeniden okunur
        # (botu yeniden başlatmadan sıra değiştirilebilir).
        try:
            mtime = self.file.stat().st_mtime
        except OSError:
            mtime = 0.0

        if self._cache is not None and mtime == self._mtime:
            return self._cache

        data = read_json(self.file, DEFAULT_PRIORITY)
        if not isinstance(data, dict):
            data = DEFAULT_PRIORITY

        self._cache = data
        self._mtime = mtime
        return data

    def for_platform(self, platform: str, *, available: set[str] | None = None) -> list[str]:
        """
        Bir platform için denenecek kaynakları sırayla döner.

        available: o an kullanılabilir kaynaklar (ör. cobalt yapılandırılmamışsa
        listede olmaz). Böylece kapalı bir kaynak için boşuna deneme yapılmaz.
        """
        data = self._load()

        platforms = data.get("platforms")
        order = _sanitize((platforms or {}).get(platform)) if isinstance(platforms, dict) else []

        if not order:
            order = _sanitize(data.get("default")) or list(DEFAULT_PRIORITY["default"])

        if available is not None:
            order = [item for item in order if item in available]

        # Hiçbir şey kalmadıysa yt-dlp'ye düş — bot her zaman bir kaynağa sahip olmalı.
        return order or ["ytdlp"]

    def describe(self) -> str:
        """Admin paneli / log için okunur özet."""
        data = self._load()
        lines = ["varsayılan: " + " → ".join(_sanitize(data.get("default")) or ["ytdlp"])]
        platforms = data.get("platforms") or {}
        if isinstance(platforms, dict):
            for name, order in sorted(platforms.items()):
                clean = _sanitize(order)
                if clean:
                    lines.append(f"{name}: " + " → ".join(clean))
        return "\n".join(lines)
