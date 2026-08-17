from __future__ import annotations

"""
Download job lifecycle: spawning worker processes, tracking them and killing
the ones that overrun their limits.
"""

import logging
import multiprocessing as mp
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any

from .config import Config
from .downloader.worker import worker_entry

logger = logging.getLogger("downloader")


class BusyError(RuntimeError):
    """No free download slot right now."""


class AlreadyRunningError(RuntimeError):
    """This user already has an active download."""


@dataclass
class Job:
    job_id: str
    user_id: int
    chat_id: int
    thread_id: int | None
    reply_to_message_id: int | None
    source_url: str
    download_dir: Path
    process: Any
    mode: str = "auto"
    created_at: float = field(default_factory=time.time)
    status_message_id: int | None = None
    done: bool = False
    cancelled: bool = False
    last_edit_at: float = 0.0
    title: str = ""
    # Safe mode and detailed logging context
    silent: bool = False
    username: str | None = None
    chat_title: str | None = None
    chat_type: str | None = None
    started_at: float = field(default_factory=time.time)
    # Watchdog state
    last_size_check: float = 0.0
    last_size: int = 0
    kill_reason: str = ""  # timeout | oversize | dead
    exited_at: float = 0.0  # when the worker was first seen dead (race guard)


class ProcessManager:
    def __init__(self, config: Config):
        self.config = config
        self.ctx = mp.get_context("spawn")
        self.queue = self.ctx.Queue()
        self.jobs: dict[str, Job] = {}
        self.active_by_user: dict[int, str] = {}

    def get_event_nowait(self) -> dict[str, Any] | None:
        try:
            return self.queue.get_nowait()
        except Empty:
            return None

    def get_active_count(self) -> int:
        return sum(
            1 for job in self.jobs.values()
            if not job.done and not job.cancelled
        )

    def get_user_active_job(self, user_id: int) -> Job | None:
        job_id = self.active_by_user.get(user_id)
        if not job_id:
            return None

        job = self.jobs.get(job_id)
        if not job or job.done or job.cancelled:
            self.active_by_user.pop(user_id, None)
            return None

        return job

    def active_download_dirs(self) -> list[Path]:
        """Directories of running jobs — the daily cleanup must skip these."""
        return [
            job.download_dir for job in self.jobs.values()
            if not job.done and not job.cancelled
        ]

    def start_download(
        self,
        *,
        user_id: int,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        url: str,
        mode: str = "auto",
        silent: bool = False,
        username: str | None = None,
        chat_title: str | None = None,
        chat_type: str | None = None,
        subtitle_lang: str = "",
    ) -> Job:
        if self.get_user_active_job(user_id):
            raise AlreadyRunningError("This user already has an active download.")

        active_count = self.get_active_count()
        limit = int(getattr(self.config, "max_simultaneous_downloads", 3) or 3)

        if active_count >= limit:
            raise BusyError(f"All {limit} download slots are busy.")

        job_id = uuid.uuid4().hex[:12]
        download_dir = self.config.download_dir / job_id

        payload = {
            "job_id": job_id,
            "url": url,
            "download_dir": str(download_dir),
            "data_dir": str(self.config.data_dir),
            "cookies_file": str(self.config.cookies_file),
            "mode": mode,
            "subtitle_lang": subtitle_lang,
            # cobalt settings are passed by value: the spawn context does not
            # share the Config object.
            "cobalt_api_url": getattr(self.config, "cobalt_api_url", "") or "",
            "cobalt_api_key": getattr(self.config, "cobalt_api_key", "") or "",
            "cobalt_timeout": getattr(self.config, "cobalt_timeout", 30),
            "job_max_bytes": getattr(self.config, "job_max_bytes", 4 * 1024**3),
        }

        process = self.ctx.Process(
            target=worker_entry,
            args=(payload, self.queue),
            daemon=True,
        )
        process.start()

        job = Job(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            source_url=url,
            download_dir=download_dir,
            process=process,
            mode=mode,
            silent=silent,
            username=username,
            chat_title=chat_title,
            chat_type=chat_type,
        )

        self.jobs[job_id] = job
        self.active_by_user[user_id] = job_id

        return job

    def attach_status_message(self, job_id: str, message_id: int) -> None:
        job = self.jobs.get(job_id)
        if job:
            job.status_message_id = message_id

    def mark_done(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        job.done = True
        self.active_by_user.pop(job.user_id, None)

    def _kill_process_tree(self, job: Job) -> None:
        """
        Kills the worker and all of its children, ffmpeg in particular.

        The worker calls os.setsid(), so it leads its own process group and the
        whole group can be signalled at once. Signalling the worker alone would
        orphan ffmpeg, which would keep writing to disk unsupervised.
        """
        proc = job.process
        if not proc:
            return

        pid = getattr(proc, "pid", None)

        if pid:
            self._signal_group(pid, signal.SIGTERM)

        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3)
        except Exception:
            pass

        if pid:
            self._signal_group(pid, signal.SIGKILL)

        try:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
        except Exception:
            pass

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return

        # Never signal the bot's own group (in case setsid failed).
        try:
            if pgid == os.getpgrp():
                return
        except OSError:
            return

        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.done or job.cancelled:
            return False

        job.cancelled = True
        job.done = True
        self.active_by_user.pop(job.user_id, None)

        self._kill_process_tree(job)

        self.cleanup_job_files(job_id)
        self._join_process(job)
        self.jobs.pop(job_id, None)

        return True

    def cancel_user_job(self, user_id: int) -> bool:
        job = self.get_user_active_job(user_id)
        if not job:
            return False
        return self.cancel_job(job.job_id)

    def cancel_chat_jobs(self, chat_id: int) -> int:
        """Cancels every active job in a chat; returns how many were cancelled."""
        targets = [
            job.job_id for job in list(self.jobs.values())
            if job.chat_id == int(chat_id) and not job.done and not job.cancelled
        ]
        return sum(1 for job_id in targets if self.cancel_job(job_id))

    # ── Watchdog ─────────────────────────────────────────────────────────────

    def _dir_size(self, path: Path) -> int:
        total = 0
        try:
            for root, _, names in os.walk(path):
                for name in names:
                    try:
                        total += (Path(root) / name).stat().st_size
                    except OSError:
                        pass
        except OSError:
            return 0
        return total

    def reap(self) -> list[tuple[Job, str]]:
        """
        Kills jobs that exceed their limits and returns (job, reason) pairs.

        Three guarantees, so no single job can lock up the bot or the server:
          1) timeout  — the job ran for too long
          2) oversize — one job is trying to fill the disk
          3) dead     — the worker crashed but its slot stayed busy
        """
        killed: list[tuple[Job, str]] = []
        now = time.time()

        timeout = int(getattr(self.config, "job_timeout_sec", 1800) or 1800)
        max_bytes = int(getattr(self.config, "job_max_bytes", 4 * 1024**3) or 4 * 1024**3)

        for job_id, job in list(self.jobs.items()):
            if job.done or job.cancelled:
                continue

            reason = ""
            age = now - job.started_at

            if age > timeout:
                reason = "timeout"

            elif job.process and not job.process.is_alive():
                # The worker exits right after putting "done" on the queue, so
                # a process seen dead may still have a pending success event.
                # Wait a little; if the event is consumed the job disappears.
                if not job.exited_at:
                    job.exited_at = now
                    continue
                if now - job.exited_at < 10.0:
                    continue
                reason = "dead"

            # The size check walks the disk, so only every 15 seconds.
            elif now - job.last_size_check > 15.0:
                job.last_size_check = now
                size = self._dir_size(job.download_dir)
                job.last_size = size
                if size > max_bytes:
                    reason = "oversize"

            if not reason:
                continue

            logger.error(
                "WATCHDOG killed job %s | reason=%s | age=%.0fs | size=%.1fMB | url=%s",
                job_id, reason, age, job.last_size / 1e6, job.source_url,
            )

            job.cancelled = True
            job.done = True
            job.kill_reason = reason
            self.active_by_user.pop(job.user_id, None)
            self._kill_process_tree(job)

            killed.append((job, reason))

        return killed

    def cleanup_job_files(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        try:
            shutil.rmtree(job.download_dir, ignore_errors=True)
        except Exception:
            pass

    def _join_process(self, job) -> None:
        # Join finished processes so no zombies or semaphores pile up.
        proc = job.process
        try:
            if proc and proc.is_alive():
                proc.join(timeout=1)
            if proc and hasattr(proc, "close"):
                proc.close()
        except Exception:
            pass

    def remove_job(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if not job:
            return

        self.active_by_user.pop(job.user_id, None)
        self._join_process(job)
        try:
            shutil.rmtree(job.download_dir, ignore_errors=True)
        except Exception:
            pass

    def detach_job(self, job_id: str) -> None:
        # Successful upload: drop the job record but keep the files, they back
        # the cache disk fallback until the daily cleanup.
        job = self.jobs.pop(job_id, None)
        if not job:
            return
        self.active_by_user.pop(job.user_id, None)
        self._join_process(job)

    def shutdown(self) -> None:
        # Called by /stop, /refresh and the panel: cancel active jobs only.
        # The queue stays open because the bot keeps running.
        for job_id in list(self.jobs.keys()):
            self.cancel_job(job_id)

    def close(self) -> None:
        # Only on full application shutdown.
        self.shutdown()
        try:
            self.queue.close()
            self.queue.cancel_join_thread()
        except Exception:
            pass
