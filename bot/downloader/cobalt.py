from __future__ import annotations

"""
bot/downloader/cobalt.py — cobalt API istemcisi.

cobalt (https://github.com/imputnet/cobalt) bir medya indirme servisidir.
Bazı platformlarda (TikTok, Instagram, Twitter/X, Reddit) yt-dlp'den daha
hızlı ve stabil sonuç verir; çünkü sunucu tarafında hazır çözümleme yapar.

ÖNEMLİ — herkese açık API yok:
    cobalt'ın resmî ortak API'si kapalıdır ("there is currently no publicly
    available pre-hosted api"). Kullanmak için kendi instance'ını çalıştırman
    gerekir (docker compose). COBALT_API_URL tanımlı değilse bu kaynak
    otomatik olarak devre dışı kalır ve pipeline yt-dlp ile devam eder.

LİSANS — AGPL-3.0:
    Bu dosya cobalt KAYNAK KODUNU İÇERMEZ; yalnızca HTTP API'sine istek atar.
    Ağ üzerinden ayrı bir servisle konuşmak türev eser oluşturmaz, dolayısıyla
    bu botun kendi lisansı AGPL'e tabi olmaz. ANCAK cobalt instance'ını sen
    barındırıp kullanıcılara hizmet olarak sunuyorsan, AGPL-3.0 §13 gereği
    cobalt'ın (yaptığın değişiklikler dahil) kaynak kodunu o kullanıcılara
    sunmakla yükümlüsün. Ayrıntı: docs/COBALT.md
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


# cobalt'ın desteklediği servisler (api/README.md, sürüm 11).
# Pipeline önceliği bu listeye göre filtrelenir: desteklenmeyen bir platform
# için cobalt'a hiç istek atılmaz.
SUPPORTED_SERVICES = {
    "bilibili", "bluesky", "dailymotion", "facebook", "instagram", "loom",
    "newgrounds", "ok", "pinterest", "reddit", "rutube", "snapchat",
    "soundcloud", "streamable", "tiktok", "tumblr", "twitch", "twitter",
    "vimeo", "vk", "youtube",
}

# Bot platform adı → cobalt servis adı
PLATFORM_TO_SERVICE = {
    "YouTube": "youtube",
    "YouTube Music": "youtube",
    "Instagram": "instagram",
    "TikTok": "tiktok",
    "Facebook": "facebook",
    "X/Twitter": "twitter",
    "Reddit": "reddit",
    "Pinterest": "pinterest",
    "SoundCloud": "soundcloud",
    "Vimeo": "vimeo",
    "Twitch": "twitch",
    "Bluesky": "bluesky",
    "Dailymotion": "dailymotion",
    "Tumblr": "tumblr",
    "Snapchat": "snapchat",
    "VK": "vk",
    "Bilibili": "bilibili",
    "Rutube": "rutube",
    "Streamable": "streamable",
    "Loom": "loom",
    "Newgrounds": "newgrounds",
    "OK.ru": "ok",
}

_FILENAME_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class CobaltError(RuntimeError):
    """cobalt isteği başarısız oldu (bir sonraki kaynağa geçilir)."""


class CobaltUnavailable(CobaltError):
    """Instance tanımsız/erişilemez — yapılandırma sorunu, içerik sorunu değil."""


def platform_supported(platform: str) -> bool:
    service = PLATFORM_TO_SERVICE.get(platform)
    return bool(service and service in SUPPORTED_SERVICES)


def _safe_filename(name: str, fallback: str = "cobalt-media") -> str:
    name = _FILENAME_SAFE.sub("_", (name or "").strip()) or fallback
    # Aşırı uzun isimler dosya sistemini zorlar (255 bayt sınırı).
    if len(name.encode("utf-8")) > 200:
        stem, dot, ext = name.rpartition(".")
        stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore")
        name = f"{stem}{dot}{ext}" if dot else stem
    return name


class CobaltClient:
    """
    Tek bir cobalt instance'ı ile konuşan basit istemci.

    Kullanım:
        client = CobaltClient(api_url="http://127.0.0.1:9000")
        files, info = client.download(url=..., download_dir=..., mode="auto")
    """

    def __init__(
        self,
        api_url: str,
        *,
        api_key: str = "",
        timeout: int = 30,
        download_timeout: int = 600,
        max_bytes: int = 4 * 1024 * 1024 * 1024,
        user_agent: str = "DownloaderBot/1.0",
    ):
        self.api_url = (api_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    @property
    def enabled(self) -> bool:
        return bool(self.api_url)

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            # cobalt iki şema kabul eder: "Api-Key <uuid>" ve "Bearer <jwt>".
            # Kullanıcı tam başlığı yazdıysa olduğu gibi kullan.
            if self.api_key.lower().startswith(("api-key ", "bearer ")):
                headers["Authorization"] = self.api_key
            else:
                headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    def _build_payload(self, url: str, mode: str, *, subtitle_lang: str = "") -> dict[str, Any]:
        """Bot modunu (video_720, audio_320, auto...) cobalt parametrelerine çevirir."""
        payload: dict[str, Any] = {
            "url": url,
            "filenameStyle": "basic",
            # Sunucu birleştirme/dönüştürmeyi kendi yapsın; biz tek bir hazır
            # dosya indirelim. "disabled" olmazsa local-processing yanıtı gelir
            # ve ffmpeg işini bize bırakır.
            "localProcessing": "disabled",
        }

        if mode.startswith("audio"):
            payload["downloadMode"] = "audio"
            payload["audioFormat"] = "mp3"
            bitrate = {
                "audio_320": "320", "audio_best": "320",
                "audio_192": "128", "audio_mp3": "128", "audio_128": "128",
            }.get(mode, "320")
            # cobalt yalnızca 320/256/128/96/64/8 kabul eder.
            payload["audioBitrate"] = bitrate
            payload["tiktokFullAudio"] = True
        else:
            payload["downloadMode"] = "auto"
            quality = {
                "video_1080": "1080", "video_720": "720",
                "video_480": "480", "video_360": "360",
                "video_best": "max", "auto": "1080",
            }.get(mode, "1080")
            payload["videoQuality"] = quality

        if subtitle_lang:
            payload["subtitleLang"] = subtitle_lang

        return payload

    def request(self, url: str, mode: str = "auto", *, subtitle_lang: str = "") -> dict[str, Any]:
        """cobalt'a POST atar ve ham JSON yanıtı döner."""
        if not self.enabled:
            raise CobaltUnavailable("cobalt instance adresi tanımlı değil (COBALT_API_URL).")

        payload = self._build_payload(url, mode, subtitle_lang=subtitle_lang)

        try:
            resp = requests.post(
                self.api_url + "/",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CobaltUnavailable(f"cobalt'a bağlanılamadı: {exc}") from exc

        try:
            data = resp.json()
        except ValueError:
            raise CobaltError(
                f"cobalt geçersiz yanıt verdi (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        if not isinstance(data, dict):
            raise CobaltError("cobalt beklenmeyen yanıt biçimi döndü.")

        status = data.get("status")

        if status == "error":
            err = data.get("error") or {}
            code = str(err.get("code") or "bilinmeyen")
            # Kimlik/oran sınırı hataları yapılandırma sorunudur.
            if code.startswith("api.auth") or "rate_exceeded" in code:
                raise CobaltUnavailable(f"cobalt reddetti: {code}")
            raise CobaltError(f"cobalt hata: {code}")

        return data

    # ── Dosya indirme ─────────────────────────────────────────────────────────

    def _download_file(self, url: str, dest: Path, *, on_progress=None) -> int:
        """Tek bir dosyayı indirir; boyut sınırını aşarsa iptal eder."""
        try:
            with requests.get(
                url,
                stream=True,
                timeout=self.download_timeout,
                headers={"User-Agent": self.user_agent},
            ) as resp:
                resp.raise_for_status()

                total = int(resp.headers.get("Content-Length") or 0)
                if total and total > self.max_bytes:
                    raise CobaltError(f"Dosya çok büyük: {total} bayt")

                written = 0
                dest.parent.mkdir(parents=True, exist_ok=True)

                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        written += len(chunk)
                        # Sonsuz/aşırı büyük akışa karşı sert kesme.
                        if written > self.max_bytes:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise CobaltError("Dosya boyut sınırını aştı, indirme kesildi.")
                        fh.write(chunk)
                        if on_progress:
                            on_progress(written, total)

                # BOŞ DOSYA KORUMASI:
                # cobalt bazı durumlarda HTTP 200 + 0 bayt döner (ör. YouTube'u
                # veri merkezi IP'sinden çekerken, session server olmadan).
                # Bunu başarı sayarsak kullanıcıya boş dosya gider ve pipeline
                # sıradaki kaynağa geçmez. Boş sonuç = hata.
                if written == 0:
                    dest.unlink(missing_ok=True)
                    raise CobaltError(
                        "cobalt boş dosya döndürdü (0 bayt) — sonraki kaynağa geçiliyor."
                    )

                return written

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise CobaltError(f"cobalt dosya indirme hatası: {exc}") from exc

    def download(
        self,
        *,
        url: str,
        download_dir: Path,
        mode: str = "auto",
        subtitle_lang: str = "",
        on_progress=None,
    ) -> tuple[list[str], dict[str, Any]]:
        """
        cobalt üzerinden indirir.

        Dönüş: (dosya_yolları, info_dict)
        Hata durumunda CobaltError yükseltir → pipeline sonraki kaynağa geçer.
        """
        data = self.request(url, mode, subtitle_lang=subtitle_lang)
        status = data.get("status")
        download_dir = Path(download_dir)
        files: list[str] = []

        if status in {"tunnel", "redirect"}:
            filename = _safe_filename(str(data.get("filename") or "cobalt-media"))
            dest = download_dir / filename
            self._download_file(str(data.get("url")), dest, on_progress=on_progress)
            files.append(str(dest))

        elif status == "picker":
            # Çoklu medya (Instagram carousel, TikTok slideshow, X çoklu görsel)
            items = data.get("picker") or []
            if not isinstance(items, list) or not items:
                raise CobaltError("cobalt picker yanıtı boş.")

            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                ext = {"photo": "jpg", "gif": "gif", "video": "mp4"}.get(
                    str(item.get("type") or "video"), "mp4"
                )
                dest = download_dir / f"cobalt-{index:02d}.{ext}"
                try:
                    self._download_file(str(item["url"]), dest, on_progress=on_progress)
                    files.append(str(dest))
                except CobaltError:
                    continue  # tek parça başarısızsa kalanları kurtar

            # Slideshow'un ses parçası varsa onu da al.
            audio_url = data.get("audio")
            if audio_url:
                dest = download_dir / _safe_filename(
                    str(data.get("audioFilename") or "cobalt-audio.mp3")
                )
                try:
                    self._download_file(str(audio_url), dest, on_progress=on_progress)
                    files.append(str(dest))
                except CobaltError:
                    pass

            if not files:
                raise CobaltError("cobalt picker içeriği indirilemedi.")

        elif status == "local-processing":
            # localProcessing="disabled" gönderdiğimiz için normalde buraya
            # düşmeyiz. Düşersek ffmpeg işini üstlenmek yerine sonraki kaynağa
            # bırakıyoruz — yt-dlp bunu zaten daha iyi yapıyor.
            raise CobaltError(
                "cobalt yerel işleme istedi (merge/remux gerekiyor) — sonraki kaynağa geçiliyor."
            )

        else:
            raise CobaltError(f"cobalt bilinmeyen durum döndü: {status}")

        # Son kontrol: yalnızca gerçekten içeriği olan dosyalar döner.
        files = [f for f in files if Path(f).is_file() and Path(f).stat().st_size > 0]
        if not files:
            raise CobaltError("cobalt kullanılabilir dosya üretmedi.")

        info = {
            "source": "cobalt",
            "cobalt_status": status,
            "service": str(data.get("service") or ""),
        }
        return files, info

    def health(self) -> dict[str, Any]:
        """Instance ayakta mı? GET / servis bilgisini döner."""
        if not self.enabled:
            raise CobaltUnavailable("cobalt adresi tanımlı değil.")
        try:
            resp = requests.get(
                self.api_url + "/",
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=10,
            )
            return resp.json()
        except Exception as exc:
            raise CobaltUnavailable(f"cobalt sağlık kontrolü başarısız: {exc}") from exc


def client_from_config(config: Any) -> CobaltClient:
    return CobaltClient(
        api_url=getattr(config, "cobalt_api_url", "") or "",
        api_key=getattr(config, "cobalt_api_key", "") or "",
        timeout=int(getattr(config, "cobalt_timeout", 30) or 30),
        max_bytes=int(getattr(config, "job_max_bytes", 4 * 1024**3) or 4 * 1024**3),
    )
