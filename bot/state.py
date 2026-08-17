from __future__ import annotations

"""
Bot run mode.

  normal      : regular operation
  safe        : silent mode. No status messages, buttons or emoji are sent;
                links are downloaded quietly and only the media is replied.
  maintenance : no downloads at all; a fixed notice is returned.

The mode is stored in data/bot_state.json and survives restarts. The
"enabled" flag is kept for the legacy /stop and /start commands.
"""

import logging
from pathlib import Path

from .storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

MODE_NORMAL = "normal"
MODE_SAFE = "safe"
MODE_MAINTENANCE = "maintenance"
VALID_MODES = {MODE_NORMAL, MODE_SAFE, MODE_MAINTENANCE}

DEFAULT_LANGUAGE = "en"


class BotState:
    """Reads and writes the mode / enabled / language flags."""

    def __init__(self, data_dir: Path):
        self.state_file = data_dir / "bot_state.json"

    def _read(self) -> dict:
        data = read_json(self.state_file, {"enabled": True, "mode": MODE_NORMAL})
        if not isinstance(data, dict):
            data = {"enabled": True, "mode": MODE_NORMAL}
        if data.get("mode") not in VALID_MODES:
            data["mode"] = MODE_NORMAL
        return data

    def _write(self, data: dict) -> None:
        write_json_atomic(self.state_file, data)

    # ── Mode ──────────────────────────────────────────────────────────────────
    def get_mode(self) -> str:
        return self._read().get("mode", MODE_NORMAL)

    def set_mode(self, mode: str) -> str:
        if mode not in VALID_MODES:
            mode = MODE_NORMAL
        data = self._read()
        previous = data.get("mode", MODE_NORMAL)
        data["mode"] = mode
        self._write(data)
        logger.info("MODE CHANGED: %s -> %s", previous, mode)
        return mode

    def is_safe(self) -> bool:
        return self.get_mode() == MODE_SAFE

    def is_maintenance(self) -> bool:
        return self.get_mode() == MODE_MAINTENANCE

    def is_normal(self) -> bool:
        return self.get_mode() == MODE_NORMAL

    # ── Enabled (shares the same file as Permissions) ─────────────────────────
    def get_enabled(self) -> bool:
        return bool(self._read().get("enabled", True))

    def set_enabled(self, enabled: bool) -> bool:
        data = self._read()
        data["enabled"] = bool(enabled)
        self._write(data)
        logger.info("BOT STATE: %s", "started" if enabled else "stopped")
        return enabled

    # ── Language ──────────────────────────────────────────────────────────────
    def get_language(self) -> str:
        return self._read().get("language", DEFAULT_LANGUAGE)

    def set_language(self, lang: str) -> str:
        data = self._read()
        previous = data.get("language", DEFAULT_LANGUAGE)
        data["language"] = lang
        self._write(data)
        logger.info("LANGUAGE CHANGED: %s -> %s", previous, lang)
        return lang
