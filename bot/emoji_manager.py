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

    (10, "icon_youtube", "▶️", "YouTube"),
    (11, "icon_ytmusic", "🎵", "YouTube Music"),
    (12, "icon_instagram", "📸", "Instagram"),
    (13, "icon_tiktok", "🎶", "TikTok"),
    (14, "icon_facebook", "📘", "Facebook"),
    (15, "icon_reddit", "🤖", "Reddit"),
    (16, "icon_pinterest", "📌", "Pinterest"),
    (17, "icon_twitter", "𝕏", "X / Twitter"),
    (18, "icon_spotify", "🎧", "Spotify"),
    (19, "icon_soundcloud", "🔊", "SoundCloud"),
    (20, "icon_vimeo", "🎬", "Vimeo"),
    (21, "icon_dailymotion", "🎞", "Dailymotion"),
    (22, "icon_twitch", "🎮", "Twitch"),
    (23, "icon_bluesky", "🦋", "Bluesky"),
    (24, "icon_tumblr", "📝", "Tumblr"),
    (25, "icon_snapchat", "👻", "Snapchat"),
    (26, "icon_vk", "🅥", "VK"),
    (27, "icon_rutube", "📺", "Rutube"),
    (28, "icon_bilibili", "📼", "Bilibili"),
    (29, "icon_rumble", "🎯", "Rumble"),
    (30, "icon_streamable", "🎥", "Streamable"),
    (31, "icon_imgur", "🖼", "Imgur"),
    (32, "icon_bandcamp", "🎸", "Bandcamp"),
    (33, "icon_mixcloud", "🎚", "Mixcloud"),
    (34, "icon_newgrounds", "🎨", "Newgrounds"),
    (35, "icon_loom", "💼", "Loom"),
    (36, "icon_okru", "🟠", "OK.ru"),
    (37, "icon_kick", "🥊", "Kick"),
    (38, "icon_link", "🔗", "Bilinmeyen platform"),

    (50, "field_title", "🎬", "Detaylar: başlık satırı"),
    (51, "field_uploader", "👤", "Detaylar: kanal satırı"),
    (52, "field_uploader_id", "🆔", "Detaylar: kanal ID satırı"),
    (53, "field_duration", "⏱", "Detaylar: süre satırı"),
    (54, "field_quality", "🎛", "Detaylar: kalite satırı"),
    (55, "field_size", "💾", "Detaylar: boyut satırı"),
    (56, "field_format", "📦", "Detaylar: format satırı"),
    (57, "field_views", "👁", "Detaylar: izlenme satırı"),
    (58, "field_likes", "👍", "Detaylar: beğeni satırı"),
    (59, "field_description", "📝", "Detaylar: açıklama satırı"),

    (70, "btn_info", "ℹ️", "Medya altı «Detaylar» butonu"),
    (71, "btn_source", "🔗", "Medya altı «Kaynak» butonu"),
    (72, "btn_emoji", "🎨", "Panel «Emoji» butonu"),

    (80, "status_searching", "🔎", "«Link analiz ediliyor» mesajı"),
    (81, "status_preparing", "⏳", "«Hazırlanıyor» mesajı"),
    (82, "status_downloading", "📥", "İndirme ilerleme mesajı"),
    (83, "status_uploading", "📤", "«Yükleniyor» mesajı"),
    (84, "status_processing", "🔄", "«Son işlemler» mesajı"),
    (85, "status_cancel", "🛑", "«İptal edildi» mesajı"),
    (86, "status_error", "❌", "Hata mesajı"),
    (87, "status_done", "✅", "Başarı mesajı"),
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
    (10, 49, "Platform ikonları"),
    (50, 69, "Bilgi alanları"),
    (70, 79, "Butonlar"),
    (80, 99, "Durum mesajları"),
]


def category_for(sid: int) -> str:
    for lo, hi, name in SLOT_CATEGORIES:
        if lo <= sid <= hi:
            return name
    return "Diğer"
