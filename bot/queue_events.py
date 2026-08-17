from __future__ import annotations

"""Event payloads sent from the worker process to the main process."""

from typing import Any


def progress_event(
    job_id: str,
    percent: float | None = None,
    downloaded: int | None = None,
    total: int | None = None,
    speed: float | None = None,
    eta: int | None = None,
    status: str = "downloading",
) -> dict[str, Any]:
    return {
        "type": "progress",
        "job_id": job_id,
        "percent": percent,
        "downloaded": downloaded,
        "total": total,
        "speed": speed,
        "eta": eta,
        "status": status,
    }


def done_event(
    job_id: str,
    files: list[str],
    title: str = "",
    source_url: str = "",
    info: dict[str, Any] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    return {
        "type": "done",
        "job_id": job_id,
        "files": files,
        "title": title,
        "source_url": source_url,
        "info": info or {},
        "mode": mode,
    }


def error_event(
    job_id: str,
    error: str,
    public_message: str = "",
    kind: str = "generic",
) -> dict[str, Any]:
    # kind: "generic" | "live" | "timeout" | "oversize"
    # Expected rejections (livestreams, timeouts) do not trigger an admin
    # failure notification.
    return {
        "type": "error",
        "job_id": job_id,
        "error": error,
        "public_message": public_message,
        "kind": kind,
    }


def cancelled_event(job_id: str) -> dict[str, Any]:
    return {
        "type": "cancelled",
        "job_id": job_id,
    }


def cookie_event(
    job_id: str,
    platform: str,
    reason: str,
    url: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Cookie related download failure, recorded by the main process."""
    return {
        "type": "cookie_error",
        "job_id": job_id,
        "platform": platform,
        "reason": reason,
        "url": url,
        "error": error,
    }


def log_event(job_id: str, level: str, message: str) -> dict[str, Any]:
    return {
        "type": "log",
        "job_id": job_id,
        "level": level,
        "message": message,
    }
