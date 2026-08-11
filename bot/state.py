from __future__ import annotations

"""
bot/state.py — Bot çalışma modu yönetimi.

Üç mod:
  - normal      : olağan çalışma
  - safe        : sessiz mod. Kullanıcıya mesaj/yazıyor/emoji gönderilmez;
                  link sessizce indirilip yalnızca medya yanıt olarak iletilir.
  - maintenance : bakım modu. Hiçbir indirme yapılmaz; sabit mesaj döner.

Mod bilgisi data/bot_state.json içinde KALICI saklanır ve yeniden başlatmada
korunur. "enabled" alanı geriye dönük uyumluluk için korunur (eski /dur /basla).
"""

import logging
from pathlib import Path

from .storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

MODE_NORMAL = "normal"
MODE_SAFE = "safe"
MODE_MAINTENANCE = "maintenance"
VALID_MODES = {MODE_NORMAL, MODE_SAFE, MODE_MAINTENANCE}

MAINTENANCE_MESSAGE = "Bot şu an bakımda, lütfen beklemede kalın."


class BotState:
    """bot_state.json üzerinde mod ve enabled durumunu yönetir."""

    def __init__(self, data_dir: Path):
        self.state_file = data_dir / "bot_state.json"

    # ── Düşük seviye okuma/yazma ──────────────────────────────────────────────
    def _read(self) -> dict:
        data = read_json(self.state_file, {"enabled": True, "mode": MODE_NORMAL})
        if not isinstance(data, dict):
            data = {"enabled": True, "mode": MODE_NORMAL}
        # Eski dosyalarda mode olmayabilir → normal kabul et
        if data.get("mode") not in VALID_MODES:
            data["mode"] = MODE_NORMAL
        return data

    def _write(self, data: dict) -> None:
        write_json_atomic(self.state_file, data)

    # ── Mod ───────────────────────────────────────────────────────────────────
    def get_mode(self) -> str:
        return self._read().get("mode", MODE_NORMAL)

    def set_mode(self, mode: str) -> str:
        """Modu değiştirir, kalıcı yazar ve loglar. Geçersizse normal'e düşer."""
        if mode not in VALID_MODES:
            mode = MODE_NORMAL
        data = self._read()
        previous = data.get("mode", MODE_NORMAL)
        data["mode"] = mode
        self._write(data)
        # Her mod değişikliğini logla
        logger.info("MOD DEĞİŞİKLİĞİ: %s -> %s", previous, mode)
        return mode

    def is_safe(self) -> bool:
        return self.get_mode() == MODE_SAFE

    def is_maintenance(self) -> bool:
        return self.get_mode() == MODE_MAINTENANCE

    def is_normal(self) -> bool:
        return self.get_mode() == MODE_NORMAL

    # ── Başlat / Durdur (enabled alanı; Permissions ile aynı dosyayı paylaşır) ─
    def get_enabled(self) -> bool:
        return bool(self._read().get("enabled", True))

    def set_enabled(self, enabled: bool) -> bool:
        data = self._read()
        data["enabled"] = bool(enabled)
        self._write(data)
        logger.info("BOT DURUMU: %s", "başlatıldı" if enabled else "durduruldu")
        return enabled

    # ── Dil ────────────────────────────────────────────────────────────────────
    def get_language(self) -> str:
        return self._read().get("language", "tr")

    def set_language(self, lang: str) -> str:
        """Bot dilini kalıcı yazar ve loglar."""
        data = self._read()
        previous = data.get("language", "tr")
        data["language"] = lang
        self._write(data)
        logger.info("DİL DEĞİŞİKLİĞİ: %s -> %s", previous, lang)
        return lang
