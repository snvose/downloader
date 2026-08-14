from __future__ import annotations

"""
bot/cookie_health.py — cookie durumu takibi ve raporlama.

İki iş yapar:

1) DOSYA ANALİZİ — cookies.txt'i platform bazında okur: hangi platformun
   çerezi var, kaç tanesi süresi dolmuş, en yakın bitiş ne zaman.

2) HATA TAKİBİ — indirme hataları içinde cookie kaynaklı olanları ayırır,
   ayrı bir log dosyasına yazar (data/logs/cookie_errors.log) ve platform
   bazında sayaç tutar (data/cookie_stats.json). Böylece admin "hangi
   cookie'yi yenilemem gerek" sorusunu tek bakışta yanıtlar.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

from .storage import read_json, write_json_atomic

logger = logging.getLogger("downloader")

# Cookie yenilenmesi gerektiğine işaret eden hata kalıpları.
# yt-dlp/gallery-dl bu mesajları giriş gerektiren ya da oturumu geçersiz
# içeriklerde döndürür.
_COOKIE_ERROR_PATTERNS = [
    # En başta: hesap kilidi cookie'yi yenilemekle DÜZELMEZ, önce hesapta
    # doğrulamanın tamamlanması gerekir. Bu yüzden aşağıdaki genel
    # kalıplardan önce yakalanmalı ki panel "cookie'yi yenile" demesin.
    (r"checkpoint_required|challenge_required|instagram\.com/challenge",
     "hesap kilitli — doğrulama gerekiyor (cookie yenilemek yetmez)"),
    (r"sign in to confirm", "giriş doğrulaması isteniyor"),
    (r"login required", "giriş gerekiyor"),
    (r"requested content is not available|content isn'?t available", "içerik oturumsuz görünmüyor"),
    (r"private (video|account|profile)", "özel içerik"),
    (r"this video is only available for registered users", "yalnızca üyelere açık"),
    (r"http error 401", "yetkisiz (401)"),
    (r"http error 403|403: forbidden", "erişim reddedildi (403)"),
    (r"age.?restricted|confirm your age", "yaş kısıtlaması"),
    # DİKKAT: burada yalnızca "cookies" aramak yanlış pozitif üretiyordu.
    # Toplu hata mesajında deneme ETİKETLERİ de duruyor ("cookies: ERROR: ...",
    # "cookies-loose: ..."); çıplak kalıp bu etiketi yakalayıp cookie ile hiç
    # ilgisi olmayan her başarısızlığı "cookie hatası" diye raporluyordu.
    # Örnek: TikTok slayt gönderisi "Unsupported URL" ile düşüyor, panel ise
    # "TikTok cookie'sini yenile" diyordu.
    (r"--cookies|cookies? (?:are |is )?(?:no longer valid|invalid|expired|"
     r"rejected|required)|invalid cookies?|cookies? (?:have )?expired",
     "cookie hatası"),
    (r"unable to download webpage.*(login|auth)", "oturum sorunu"),
    (r"rate.?limit|too many requests", "oran sınırı (oturum yenilenmeli)"),
    (r"empty media response", "boş yanıt (oturum düşmüş olabilir)"),
    (r"unable to extract (shared_data|sharedData|viewer)", "oturum çerezi geçersiz"),
]

# Hangi platform hangi cookie alan adına ihtiyaç duyar.
PLATFORM_DOMAINS = {
    "YouTube": ["youtube.com"],
    "YouTube Music": ["youtube.com"],
    "Instagram": ["instagram.com"],
    "TikTok": ["tiktok.com"],
    "Facebook": ["facebook.com"],
    "X/Twitter": ["x.com", "twitter.com"],
    "Reddit": ["reddit.com"],
    "Pinterest": ["pinterest.com"],
    "Spotify": ["spotify.com"],
}

# Bu platformlar giriş olmadan da çalışır; cookie yoksa "eksik" denmez.
_OPTIONAL_COOKIE_PLATFORMS = {
    "YouTube", "YouTube Music", "Reddit", "Pinterest", "Spotify",
}

EXPIRY_WARN_DAYS = 7


# Hata metnindeki extractor/alan adı izlerinden gerçek platformu çıkarmak
# için kullanılır. Bir platformun indirmesi başka bir platform üzerinden
# yürüyebilir (Spotify -> YouTube araması gibi); bu durumda yenilenmesi
# gereken cookie, linkin platformununki DEĞİLDİR.
_ERROR_PLATFORM_MARKERS = [
    (r"\[youtube(:search)?\]|youtube\.com|ytsearch", "YouTube"),
    (r"\[instagram\]|instagram\.com", "Instagram"),
    (r"\[tiktok\]|tiktok\.com", "TikTok"),
    (r"\[facebook\]|facebook\.com", "Facebook"),
    (r"\[twitter\]|\[x\]|twitter\.com|(?<!\w)x\.com", "X/Twitter"),
    (r"\[reddit\]|reddit\.com", "Reddit"),
    (r"\[pinterest\]|pinterest\.com", "Pinterest"),
]


def error_platform_hint(message: str) -> str | None:
    """
    Hata mesajının hangi platformun cookie'sine işaret ettiğini döndürür.

    Spotify indirmeleri YouTube araması üzerinden yürüdüğü için, hata
    "Spotify" işi altında gelse bile yenilenmesi gereken cookie YouTube'un
    olabilir. Bu ayrım yapılmazsa admin paneli yanlış platformu işaret eder.
    """
    if not message:
        return None

    lowered = str(message).lower()
    for pattern, platform in _ERROR_PLATFORM_MARKERS:
        if re.search(pattern, lowered):
            return platform
    return None


def classify_cookie_error(message: str) -> str | None:
    """
    Hata mesajı cookie kaynaklı mı? Değilse None, ise okunur sebep döner.
    """
    if not message:
        return None

    lowered = str(message).lower()
    for pattern, reason in _COOKIE_ERROR_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return None


# ── 1. Dosya analizi ─────────────────────────────────────────────────────────

def parse_cookie_file(path: Path) -> dict[str, dict[str, Any]]:
    """
    Netscape cookies.txt'i alan adı bazında özetler.

    Dönüş: {"tiktok.com": {"count": 22, "expired": 0, "nearest_expiry": ts,
                           "names": [...]}}
    """
    path = Path(path)
    result: dict[str, dict[str, Any]] = {}

    if not path.exists():
        return result

    now = time.time()

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()
        if len(parts) < 7:
            continue

        domain = parts[0].lstrip(".").lower()
        try:
            expiry = int(parts[4])
        except (TypeError, ValueError):
            expiry = 0
        name = parts[5]

        entry = result.setdefault(domain, {
            "count": 0, "expired": 0, "nearest_expiry": 0, "names": [],
        })
        entry["count"] += 1
        if name not in entry["names"]:
            entry["names"].append(name)

        if expiry > 0:
            if expiry < now:
                entry["expired"] += 1
            else:
                current = entry["nearest_expiry"]
                entry["nearest_expiry"] = expiry if not current else min(current, expiry)

    return result


def platform_cookie_status(
    cookies_file: Path,
    *,
    failures: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Her platform için cookie durumunu üretir (admin paneli bunu gösterir).

    status: ok | expiring | expired | missing | optional_missing
    """
    domains = parse_cookie_file(cookies_file)
    failures = failures or {}
    now = time.time()
    rows: list[dict[str, Any]] = []

    for platform, wanted in PLATFORM_DOMAINS.items():
        matched: dict[str, Any] | None = None

        # Alt alan adları da sayılır (www.tiktok.com → tiktok.com)
        for domain, entry in domains.items():
            if any(domain == w or domain.endswith("." + w) for w in wanted):
                if matched is None:
                    matched = {
                        "count": 0, "expired": 0, "nearest_expiry": 0,
                    }
                matched["count"] += entry["count"]
                matched["expired"] += entry["expired"]
                nearest = entry["nearest_expiry"]
                if nearest:
                    matched["nearest_expiry"] = (
                        nearest if not matched["nearest_expiry"]
                        else min(matched["nearest_expiry"], nearest)
                    )

        fail_count = int((failures.get(platform) or {}).get("count", 0))
        last_reason = str((failures.get(platform) or {}).get("reason", ""))
        last_time = float((failures.get(platform) or {}).get("last", 0))

        if not matched:
            status = (
                "optional_missing" if platform in _OPTIONAL_COOKIE_PLATFORMS else "missing"
            )
            rows.append({
                "platform": platform, "status": status, "count": 0,
                "expired": 0, "nearest_expiry": 0, "days_left": None,
                "failures": fail_count, "last_reason": last_reason,
                "last_failure": last_time,
            })
            continue

        nearest = matched["nearest_expiry"]
        days_left = int((nearest - now) / 86400) if nearest else None

        if matched["expired"] and matched["expired"] >= matched["count"]:
            status = "expired"
        elif days_left is not None and days_left <= EXPIRY_WARN_DAYS:
            status = "expiring"
        else:
            status = "ok"

        rows.append({
            "platform": platform,
            "status": status,
            "count": matched["count"],
            "expired": matched["expired"],
            "nearest_expiry": nearest,
            "days_left": days_left,
            "failures": fail_count,
            "last_reason": last_reason,
            "last_failure": last_time,
        })

    # Sorunlular üstte: expired → missing → expiring → ok
    order = {"expired": 0, "missing": 1, "expiring": 2, "optional_missing": 3, "ok": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["failures"]))
    return rows


# ── 2. Hata takibi ───────────────────────────────────────────────────────────

class CookieLog:
    """
    Cookie kaynaklı indirme hatalarını ayrı bir kanala yazar ve sayar.

    Dosyalar:
        data/logs/cookie_errors.log — insan tarafından okunur ayrıntılı kayıt
        data/cookie_stats.json      — admin paneli için platform sayaçları
    """

    def __init__(self, data_dir: Path, log_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.log_dir = Path(log_dir) if log_dir else self.data_dir / "logs"
        self.log_file = self.log_dir / "cookie_errors.log"
        self.stats_file = self.data_dir / "cookie_stats.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.stats_file, {"platforms": {}})
        if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
            return {"platforms": {}}
        return data

    def record(
        self,
        *,
        platform: str,
        reason: str,
        url: str = "",
        error: str = "",
        user_id: int | None = None,
    ) -> None:
        """Cookie kaynaklı bir hatayı kaydeder."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        domains = ", ".join(PLATFORM_DOMAINS.get(platform, [])) or "-"

        # 1) Ayrıntılı log satırı
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            line = (
                f"{timestamp} | platform={platform} | gereken_cookie={domains} "
                f"| sebep={reason} | user={user_id or '-'} | url={url[:160]} "
                f"| hata={str(error)[:300].replace(chr(10), ' ')}\n"
            )
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            logger.warning("Cookie log yazılamadı: %s", exc)

        # 2) Panel sayacı
        try:
            data = self._load()
            entry = data["platforms"].setdefault(platform, {"count": 0})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["reason"] = reason
            entry["last"] = time.time()
            entry["last_url"] = url[:200]
            write_json_atomic(self.stats_file, data)
        except Exception as exc:
            logger.warning("Cookie istatistiği yazılamadı: %s", exc)

    def failures(self) -> dict[str, Any]:
        return self._load().get("platforms", {})

    def total(self) -> int:
        return sum(
            int(v.get("count", 0))
            for v in self.failures().values()
            if isinstance(v, dict)
        )

    def reset(self, platform: str | None = None) -> None:
        data = self._load()
        if platform:
            data["platforms"].pop(platform, None)
        else:
            data["platforms"] = {}
        write_json_atomic(self.stats_file, data)

    def tail(self, lines: int = 15) -> list[str]:
        """Son N cookie hatası (admin paneli detayı için)."""
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        return [ln for ln in content.splitlines() if ln.strip()][-lines:]
