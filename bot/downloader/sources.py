from __future__ import annotations

"""
Download source priority.

The bot can download from three sources:
    cobalt    — your self-hosted cobalt instance (fast, server-side)
    ytdlp     — yt-dlp (widest platform support)
    gallerydl — gallery-dl (strong for galleries / multi-image posts)

The order is NOT hard-coded; it's read from data/sources.json and can be
overridden per platform. If the file is missing, the defaults below are used
and the file is created.

Example data/sources.json:
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

# ── Default priorities ───────────────────────────────────────────────────────
# Rationale:
#   TikTok/Instagram/Twitter/Reddit/Pinterest -> cobalt first: these platforms
#     change their HTML/signing often; cobalt stays current server-side and
#     returns watermark-free / multi-item (picker) results in one request.
#   YouTube/YouTube Music -> yt-dlp first: format selection, subtitles,
#     playlists and audio quality control are far more detailed in yt-dlp,
#     and the bot's menu depends on that.
#   Everything else -> yt-dlp first (widest coverage), then cobalt.
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
    """Drops unknown/duplicate source names."""
    if not isinstance(order, list):
        return []
    clean: list[str] = []
    for item in order:
        name = str(item).strip().lower()
        if name in KNOWN_SOURCES and name not in clean:
            clean.append(name)
    return clean


class SourcePriority:
    """Reads data/sources.json and resolves platform -> source order."""

    def __init__(self, data_dir: Path):
        self.file = Path(data_dir) / "sources.json"
        self._cache: dict[str, Any] | None = None
        self._mtime: float = 0.0

    def ensure_file(self) -> None:
        if not self.file.exists():
            write_json_atomic(self.file, DEFAULT_PRIORITY)

    def _load(self) -> dict[str, Any]:
        # Re-read by mtime so the file can be edited by hand without
        # restarting the bot.
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
        Returns the sources to try, in order, for a platform.

        available: sources currently usable (e.g. cobalt is excluded when not
        configured), so a disabled source is never tried in vain.
        """
        data = self._load()

        platforms = data.get("platforms")
        order = _sanitize((platforms or {}).get(platform)) if isinstance(platforms, dict) else []

        if not order:
            order = _sanitize(data.get("default")) or list(DEFAULT_PRIORITY["default"])

        if available is not None:
            order = [item for item in order if item in available]

        # Nothing left: fall back to yt-dlp, the bot must always have a source.
        return order or ["ytdlp"]

    def describe(self) -> str:
        """Readable summary for the admin panel / logs."""
        data = self._load()
        lines = ["default: " + " → ".join(_sanitize(data.get("default")) or ["ytdlp"])]
        platforms = data.get("platforms") or {}
        if isinstance(platforms, dict):
            for name, order in sorted(platforms.items()):
                clean = _sanitize(order)
                if clean:
                    lines.append(f"{name}: " + " → ".join(clean))
        return "\n".join(lines)
