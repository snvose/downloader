from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
EMOJI_FILE = BASE_DIR / "data" / "emoji_slots.json"


SLOT_DEFS: list[tuple[int, str, str, str]] = [
    (1, "brand", "⚡", "/start başlığı"),
    (2, "menu_help", "📖", "«Yardım» butonu"),
    (3, "menu_owner", "⚙️", "«Admin Paneli» butonu"),
    (4, "menu_owner_link", "👤", "«Owner» link butonu"),
    (5, "menu_mifix", "💬", "«Topluluk» link butonu"),
    (6, "menu_back", "‹", "«Geri» butonu"),

    (10, "icon_youtube", "▶️", "YouTube platform ikonu"),
    (11, "icon_ytmusic", "🎵", "YouTube Music platform ikonu"),
    (12, "icon_instagram", "📸", "Instagram platform ikonu"),
    (13, "icon_tiktok", "🎵", "TikTok platform ikonu"),
    (14, "icon_facebook", "📘", "Facebook platform ikonu"),
    (15, "icon_reddit", "🤖", "Reddit platform ikonu"),
    (16, "icon_pinterest", "📌", "Pinterest platform ikonu"),
    (17, "icon_twitter", "𝕏", "X/Twitter platform ikonu"),
    (18, "icon_spotify", "🎧", "Spotify platform ikonu"),
    (19, "icon_link", "🔗", "Genel link ikonu"),

    (30, "field_title", "🎬", "Detaylar: başlık satırı"),
    (31, "field_uploader", "👤", "Detaylar: kanal satırı"),
    (32, "field_uploader_id", "🆔", "Detaylar: kanal ID satırı"),
    (33, "field_duration", "⏱", "Detaylar: süre satırı"),
    (34, "field_quality", "🎛", "Detaylar: kalite satırı"),
    (35, "field_size", "💾", "Detaylar: boyut satırı"),
    (36, "field_format", "📦", "Detaylar: format satırı"),
    (37, "field_views", "👁", "Detaylar: izlenme satırı"),
    (38, "field_likes", "👍", "Detaylar: beğeni satırı"),
    (39, "field_description", "📝", "Detaylar: açıklama satırı"),

    (50, "btn_info", "ℹ️", "Medya altı «Detaylar» butonu"),
    (52, "btn_source", "🔗", "Medya altı «Kaynak» butonu"),
    (56, "btn_emoji", "🎨", "Panel «Emoji» butonu"),

    (70, "status_searching", "🔎", "«Link analiz ediliyor» mesajı"),
    (71, "status_preparing", "⏳", "«Hazırlanıyor» mesajı"),
    (72, "status_downloading", "📥", "İndirme ilerleme mesajı"),
    (73, "status_uploading", "📤", "«Yükleniyor» mesajı"),
    (74, "status_processing", "🔄", "«Son işlemler» mesajı"),
    (75, "status_cancel", "🛑", "«İptal edildi» mesajı"),
    (76, "status_error", "❌", "Hata mesajı"),
    (77, "status_done", "✅", "Başarı mesajı"),
]

# NOT: 51 (btn_desc), 53 (btn_stop), 54 (btn_start), 55 (btn_refresh),
# 40 (field_platform) ve 90-94 (owner_*) slotları kaldırıldı. Bunlar
# kaldırılmış "Owner Settings" menüsüne ve artık render edilmeyen butonlara
# aitti; panelde atanabiliyor ama HİÇBİR YERDE görünmüyorlardı.


ID_TO_SLOT = {sid: (key, fb, ctx) for sid, key, fb, ctx in SLOT_DEFS}
KEY_TO_SLOT = {key: (sid, fb, ctx) for sid, key, fb, ctx in SLOT_DEFS}


def default_slots() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "fallback": fb,
            "context": ctx,
            "custom_id": None,
        }
        for _, key, fb, ctx in SLOT_DEFS
    }


# em() her emoji render'ında çağrılır. Diski her seferinde okumamak için
# dosya mtime'ına göre bellekte cache tutulur (blocking I/O'yu azaltır).
_slots_cache: dict[str, dict[str, Any]] | None = None
_slots_cache_mtime: float = -1.0


def load_slots() -> dict[str, dict[str, Any]]:
    global _slots_cache, _slots_cache_mtime

    try:
        mtime = EMOJI_FILE.stat().st_mtime if EMOJI_FILE.exists() else -1.0
    except OSError:
        mtime = -1.0

    if _slots_cache is not None and mtime == _slots_cache_mtime:
        return _slots_cache  # değişmemiş → diske gitmeye gerek yok

    data = default_slots()

    try:
        if EMOJI_FILE.exists():
            raw = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key not in data:
                        continue

                    if isinstance(value, dict):
                        custom_id = str(value.get("custom_id") or "").strip()
                    else:
                        custom_id = str(value or "").strip()

                    data[key]["custom_id"] = custom_id if custom_id.isdigit() else None
    except Exception:
        pass

    _slots_cache = data
    _slots_cache_mtime = mtime
    return data


def save_slots(data: dict[str, Any]) -> None:
    global _slots_cache, _slots_cache_mtime
    _slots_cache = None  # yazımdan sonra cache'i geçersiz kıl
    _slots_cache_mtime = -1.0
    full = default_slots()

    for key, value in (data or {}).items():
        if key not in full:
            continue

        if isinstance(value, dict):
            custom_id = str(value.get("custom_id") or "").strip()
        else:
            custom_id = str(value or "").strip()

        full[key]["custom_id"] = custom_id if custom_id.isdigit() else None

    EMOJI_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMOJI_FILE.write_text(
        json.dumps(full, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_file() -> None:
    if not EMOJI_FILE.exists():
        save_slots(default_slots())


def fallback(key: str, default: str = "") -> str:
    slot = KEY_TO_SLOT.get(key)
    return slot[1] if slot else default


def em(key: str, default: str | None = None) -> str:
    fb = fallback(key, default or "")

    try:
        custom_id = load_slots().get(key, {}).get("custom_id")
        if custom_id and str(custom_id).isdigit():
            # Telegram tg-emoji içinde gerçek emoji karakteri bekler.
            # Slot fallback'i "‹" veya "𝕏" gibi sembol olursa Entity_text_invalid verebilir.
            return f'<tg-emoji emoji-id="{custom_id}">✨</tg-emoji>'
    except Exception:
        pass

    return fb


def eb(key: str, text: str = "") -> str:
    # InlineKeyboardButton text parse_mode desteklemez.
    # Bu yüzden butonlarda premium entity değil fallback emoji kullanılır.
    fb = fallback(key, "")
    return f"{fb} {text}".strip()


def set_slot(key: str, custom_id: str) -> None:
    if key not in KEY_TO_SLOT:
        raise KeyError(key)

    custom_id = str(custom_id or "").strip()
    if not custom_id.isdigit():
        raise ValueError("custom_id sayısal olmalı")

    data = load_slots()
    data[key]["custom_id"] = custom_id
    save_slots(data)


def reset_slot(key: str) -> None:
    if key not in KEY_TO_SLOT:
        raise KeyError(key)

    data = load_slots()
    data[key]["custom_id"] = None
    save_slots(data)


def reset_all_slots() -> int:
    """Tüm slotların premium emoji atamasını kaldırır. Sıfırlanan slot sayısını döner."""
    data = load_slots()
    count = sum(1 for v in data.values() if v.get("custom_id"))
    for key in data:
        data[key]["custom_id"] = None
    save_slots(data)
    return count


def assigned_count() -> int:
    return sum(1 for value in load_slots().values() if value.get("custom_id"))


# Slot ID aralıklarına göre kategori başlıkları (panelde gruplama için)
SLOT_CATEGORIES: list[tuple[int, int, str]] = [
    (1, 9, "Menü"),
    (10, 29, "Platform ikonları"),
    (30, 49, "Bilgi alanları"),
    (50, 69, "Butonlar"),
    (70, 89, "Durum mesajları"),
]


def category_for(sid: int) -> str:
    for lo, hi, name in SLOT_CATEGORIES:
        if lo <= sid <= hi:
            return name
    return "Diğer"
