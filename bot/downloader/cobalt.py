from __future__ import annotations

"""
cobalt API client.

cobalt (https://github.com/imputnet/cobalt) is a media download service. On
some platforms (TikTok, Instagram, Twitter/X, Reddit) it's faster and more
stable than yt-dlp because the resolving happens server-side.

IMPORTANT — no public API:
    cobalt has no official shared API ("there is currently no publicly
    available pre-hosted api"). You must run your own instance (docker
    compose). If COBALT_API_URL is empty, this source is automatically
    disabled and the pipeline continues with yt-dlp.

LICENSE — AGPL-3.0:
    This file does NOT contain cobalt's source code; it only makes HTTP
    requests to its API. Talking to a separate service over the network does
    not create a derivative work, so this bot's own license is not bound by
    AGPL. HOWEVER, if you host a cobalt instance yourself and offer it as a
    service to users, AGPL-3.0 §13 requires you to make cobalt's source code
    (including your changes) available to those users. Details: docs/COBALT.md
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


# Services cobalt supports (api/README.md, version 11).
# The pipeline priority is filtered against this list, so an unsupported
# platform is never sent to cobalt at all.
SUPPORTED_SERVICES = {
    "bilibili", "bluesky", "dailymotion", "facebook", "instagram", "loom",
    "newgrounds", "ok", "pinterest", "reddit", "rutube", "snapchat",
    "soundcloud", "streamable", "tiktok", "tumblr", "twitch", "twitter",
    "vimeo", "vk", "youtube",
}

# Bot platform name -> cobalt service name
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
    """The cobalt request failed (the pipeline moves to the next source)."""


class CobaltUnavailable(CobaltError):
    """Instance not configured/unreachable — a config issue, not a content one."""


def platform_supported(platform: str) -> bool:
    service = PLATFORM_TO_SERVICE.get(platform)
    return bool(service and service in SUPPORTED_SERVICES)


def _safe_filename(name: str, fallback: str = "cobalt-media") -> str:
    name = _FILENAME_SAFE.sub("_", (name or "").strip()) or fallback
    # Overly long names break the filesystem (255-byte limit).
    if len(name.encode("utf-8")) > 200:
        stem, dot, ext = name.rpartition(".")
        stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore")
        name = f"{stem}{dot}{ext}" if dot else stem
    return name


class CobaltClient:
    """
    A simple client that talks to a single cobalt instance.

    Usage:
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

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            # cobalt accepts two schemes: "Api-Key <uuid>" and "Bearer <jwt>".
            # If the user already wrote the full header, use it as-is.
            if self.api_key.lower().startswith(("api-key ", "bearer ")):
                headers["Authorization"] = self.api_key
            else:
                headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    def _build_payload(self, url: str, mode: str, *, subtitle_lang: str = "") -> dict[str, Any]:
        """Translates the bot's mode (video_720, audio_320, auto...) into cobalt params."""
        payload: dict[str, Any] = {
            "url": url,
            "filenameStyle": "basic",
            # Let the server do the merging/converting and hand us one ready
            # file. Without "disabled" we'd get a local-processing response
            # and have to run ffmpeg ourselves.
            "localProcessing": "disabled",
        }

        if mode.startswith("audio"):
            payload["downloadMode"] = "audio"
            payload["audioFormat"] = "mp3"
            bitrate = {
                "audio_320": "320", "audio_best": "320",
                "audio_192": "128", "audio_mp3": "128", "audio_128": "128",
            }.get(mode, "320")
            # cobalt only accepts 320/256/128/96/64/8.
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
        """POSTs to cobalt and returns the raw JSON response."""
        if not self.enabled:
            raise CobaltUnavailable("No cobalt instance address configured (COBALT_API_URL).")

        # cobalt's audioFormat list has no flac. A request would still
        # silently return mp3, and handing the user mp3 when they asked for
        # FLAC is worse than not trying at all — this mode defers to yt-dlp.
        if mode == "audio_flac":
            raise CobaltUnavailable("cobalt cannot produce FLAC — deferring to yt-dlp.")

        payload = self._build_payload(url, mode, subtitle_lang=subtitle_lang)

        try:
            resp = requests.post(
                self.api_url + "/",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CobaltUnavailable(f"Could not connect to cobalt: {exc}") from exc

        try:
            data = resp.json()
        except ValueError:
            raise CobaltError(
                f"cobalt returned an invalid response (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        if not isinstance(data, dict):
            raise CobaltError("cobalt returned an unexpected response shape.")

        status = data.get("status")

        if status == "error":
            err = data.get("error") or {}
            code = str(err.get("code") or "unknown")
            # Auth/rate-limit errors are a configuration problem.
            if code.startswith("api.auth") or "rate_exceeded" in code:
                raise CobaltUnavailable(f"cobalt rejected the request: {code}")
            raise CobaltError(f"cobalt error: {code}")

        return data

    # ── File download ───────────────────────────────────────────────────────

    def _download_file(self, url: str, dest: Path, *, on_progress=None) -> int:
        """Downloads a single file; aborts if it exceeds the size limit."""
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
                    raise CobaltError(f"File too large: {total} bytes")

                written = 0
                dest.parent.mkdir(parents=True, exist_ok=True)

                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        written += len(chunk)
                        # Hard cutoff against an endless/oversized stream.
                        if written > self.max_bytes:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise CobaltError("File exceeded the size limit, download aborted.")
                        fh.write(chunk)
                        if on_progress:
                            on_progress(written, total)

                # EMPTY FILE GUARD:
                # cobalt sometimes returns HTTP 200 + 0 bytes (e.g. fetching
                # YouTube from a datacenter IP without a session server). If
                # we counted that as success the user would get an empty
                # file and the pipeline wouldn't move to the next source.
                if written == 0:
                    dest.unlink(missing_ok=True)
                    raise CobaltError(
                        "cobalt returned an empty file (0 bytes) — moving to the next source."
                    )

                return written

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise CobaltError(f"cobalt file download error: {exc}") from exc

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
        Downloads through cobalt.

        Returns (file_paths, info_dict). Raises CobaltError on failure so the
        pipeline moves on to the next source.
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
            # Multiple media items (Instagram carousel, TikTok slideshow, X
            # multi-image posts).
            items = data.get("picker") or []
            if not isinstance(items, list) or not items:
                raise CobaltError("cobalt picker response was empty.")

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
                    continue  # save the rest even if one item fails

            # Slideshows may have an audio track too.
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
                raise CobaltError("cobalt picker content could not be downloaded.")

        elif status == "local-processing":
            # We normally never hit this because localProcessing="disabled"
            # is sent. If we do, defer to the next source instead of taking
            # on the merge/remux ourselves — yt-dlp already does that better.
            raise CobaltError(
                "cobalt requested local processing (merge/remux needed) — moving to the next source."
            )

        else:
            raise CobaltError(f"cobalt returned an unknown status: {status}")

        # Final check: only keep files that actually have content.
        files = [f for f in files if Path(f).is_file() and Path(f).stat().st_size > 0]
        if not files:
            raise CobaltError("cobalt produced no usable file.")

        info = {
            "source": "cobalt",
            "cobalt_status": status,
            "service": str(data.get("service") or ""),
        }
        return files, info

    def health(self) -> dict[str, Any]:
        """Is the instance up? Returns the GET / service info."""
        if not self.enabled:
            raise CobaltUnavailable("No cobalt address configured.")
        try:
            resp = requests.get(
                self.api_url + "/",
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=10,
            )
            return resp.json()
        except Exception as exc:
            raise CobaltUnavailable(f"cobalt health check failed: {exc}") from exc


def client_from_config(config: Any) -> CobaltClient:
    return CobaltClient(
        api_url=getattr(config, "cobalt_api_url", "") or "",
        api_key=getattr(config, "cobalt_api_key", "") or "",
        timeout=int(getattr(config, "cobalt_timeout", 30) or 30),
        max_bytes=int(getattr(config, "job_max_bytes", 4 * 1024**3) or 4 * 1024**3),
    )
