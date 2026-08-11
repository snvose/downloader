from __future__ import annotations

"""
bot/live_guard.py — Canlı yayın (livestream) koruması.

Neden var:
  Canlı yayın linkleri sonsuz akış üretir. yt-dlp bunları ffmpeg alt
  sürecine devreder; indirme asla bitmez, worker slotu sonsuza dek dolu
  kalır ve disk sürekli büyür (ölçüm: ~9 MB/dk, yayın başına, süresiz).
  Bu modül indirme BAŞLAMADAN önce yayını tespit eder ve reddeder.

İki parça:
  1) probe_is_live()  — hızlı metadata sorgusu (indirme yok, ~1-2 sn)
  2) LiveGuard        — tekrar eden denemeler için uyarı + geçici ban
"""

import time
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic


# ── 1. Canlı yayın tespiti ───────────────────────────────────────────────────

# Bu alanların herhangi biri yayının canlı olduğunu gösterir.
_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live"}


def info_is_live(info: dict[str, Any] | None) -> bool:
    """Bir yt-dlp info dict'i canlı yayına mı ait?"""
    if not isinstance(info, dict):
        return False

    if info.get("is_live") is True:
        return True

    if str(info.get("live_status") or "") in _LIVE_STATUSES:
        return True

    # Playlist/çoklu girdi: herhangi bir girdi canlıysa tamamı reddedilir.
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries[:20]:
            if isinstance(entry, dict) and info_is_live(entry):
                return True

    return False


def probe_is_live(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: int = 15,
) -> tuple[bool, dict[str, Any]]:
    """
    İndirme YAPMADAN linkin canlı yayın olup olmadığını sorar.

    Dönüş: (canlı_mı, info_dict). Sorgu başarısız olursa (False, {}) döner —
    yani belirsizlik indirmeyi engellemez, normal akış hata yönetimine düşer.
    """
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": timeout,
        "retries": 1,
        # extract_flat: canlı tespiti için tam işleme gerekmez, bu da hızlandırır.
        "extract_flat": "in_playlist",
    }

    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = str(cookies_file)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception:
        return False, {}

    if not isinstance(info, dict):
        return False, {}

    return info_is_live(info), info


# ── 2. Uyarı + geçici ban ────────────────────────────────────────────────────

class LiveGuard:
    """
    Canlı yayın linki gönderen kullanıcıyı kademeli olarak kısıtlar.

    1. ve 2. deneme → uyarı mesajı.
    3. deneme       → `ban_days` gün geçici ban (süresi dolunca kendiliğinden kalkar).

    Durum data/temp_bans.json içinde tutulur:
        {"users": {"<id>": {"strikes": 2, "until": 0.0, "last": 172...}}}
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        strike_limit: int = 3,
        ban_days: int = 5,
        strike_window_days: int = 7,
    ):
        self.file = Path(data_dir) / "temp_bans.json"
        self.strike_limit = int(strike_limit)
        self.ban_days = int(ban_days)
        self.strike_window = int(strike_window_days) * 86400

    # ── dosya erişimi ──
    def _load(self) -> dict[str, dict]:
        data = read_json(self.file, {"users": {}})
        if not isinstance(data, dict):
            return {}
        users = data.get("users")
        return users if isinstance(users, dict) else {}

    def _save(self, users: dict[str, dict]) -> None:
        write_json_atomic(self.file, {"users": users})

    # ── sorgu ──
    def ban_remaining(self, user_id: int | None) -> float:
        """Kalan ban süresi (saniye). 0 ise banlı değil. Süresi dolan ban silinir."""
        if not user_id:
            return 0.0

        users = self._load()
        record = users.get(str(user_id))
        if not isinstance(record, dict):
            return 0.0

        until = float(record.get("until") or 0.0)
        remaining = until - time.time()

        if remaining <= 0:
            # Süresi dolmuş → ban kalkar, strike sayacı da sıfırlanır.
            if until:
                record["until"] = 0.0
                record["strikes"] = 0
                users[str(user_id)] = record
                self._save(users)
            return 0.0

        return remaining

    def is_banned(self, user_id: int | None) -> bool:
        return self.ban_remaining(user_id) > 0

    # ── kayıt ──
    def register_attempt(self, user_id: int) -> dict[str, Any]:
        """
        Canlı yayın denemesini kaydeder.

        Dönüş:
          {"action": "warn",   "strikes": n, "remaining": kalan_hak}
          {"action": "banned", "strikes": n, "days": gün, "seconds": saniye}
        """
        users = self._load()
        key = str(user_id)
        record = users.get(key) if isinstance(users.get(key), dict) else {}

        now = time.time()
        strikes = int(record.get("strikes") or 0)
        last = float(record.get("last") or 0.0)

        # Uzun süre temiz kalan kullanıcının sayacı sıfırlanır.
        if last and (now - last) > self.strike_window:
            strikes = 0

        strikes += 1
        record["strikes"] = strikes
        record["last"] = now

        if strikes >= self.strike_limit:
            seconds = self.ban_days * 86400
            record["until"] = now + seconds
            record["strikes"] = strikes
            users[key] = record
            self._save(users)
            return {
                "action": "banned",
                "strikes": strikes,
                "days": self.ban_days,
                "seconds": seconds,
            }

        record["until"] = 0.0
        users[key] = record
        self._save(users)
        return {
            "action": "warn",
            "strikes": strikes,
            "remaining": self.strike_limit - strikes,
        }

    def clear(self, user_id: int) -> bool:
        """Admin için: kullanıcının canlı-yayın banını ve sayacını sıfırla."""
        users = self._load()
        if str(user_id) not in users:
            return False
        users.pop(str(user_id), None)
        self._save(users)
        return True

    def list_active(self) -> list[dict[str, Any]]:
        """Aktif geçici banlar (admin paneli için)."""
        now = time.time()
        out: list[dict[str, Any]] = []
        for key, record in self._load().items():
            if not isinstance(record, dict):
                continue
            until = float(record.get("until") or 0.0)
            if until > now:
                out.append({
                    "user_id": int(key) if key.lstrip("-").isdigit() else key,
                    "remaining": until - now,
                    "strikes": int(record.get("strikes") or 0),
                })
        return sorted(out, key=lambda x: -x["remaining"])


def guard_message(result: dict[str, Any]) -> str:
    """
    register_attempt() sonucunu kullanıcıya gösterilecek metne çevirir.

    Uyarı aşamasında kalan hak belirtilir; ban aşamasında süre yazılır.
    """
    from .i18n import t

    if result.get("action") == "banned":
        return t("live_temp_banned", days=int(result.get("days", 5)))

    remaining = int(result.get("remaining", 0))
    if remaining <= 1:
        return t("live_last_warning")
    return t("live_not_supported")


def format_duration(seconds: float) -> str:
    """Kalan ban süresini kullanıcıya okunur biçimde yazar."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        return f"{days} gün {hours} saat" if hours else f"{days} gün"
    if hours:
        return f"{hours} saat {minutes} dakika" if minutes else f"{hours} saat"
    return f"{max(1, minutes)} dakika"
