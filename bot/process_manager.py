from __future__ import annotations

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
    # safe mode (sessiz gönderim) + detaylı loglama için bağlam
    silent: bool = False                 # safe mode → kullanıcıya mesaj/bildirim yok
    username: str | None = None
    chat_title: str | None = None
    chat_type: str | None = None
    started_at: float = field(default_factory=time.time)  # işlem süresi ölçümü
    # watchdog durumu
    last_size_check: float = 0.0
    last_size: int = 0
    kill_reason: str = ""  # timeout | oversize | dead
    exited_at: float = 0.0  # worker süreci ölü görüldüğü an (yarış koruması)


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
        current = self.get_user_active_job(user_id)
        if current:
            raise RuntimeError("Bu kullanıcının aktif indirmesi var.")

        active_count = self.get_active_count()
        limit = int(getattr(self.config, "max_simultaneous_downloads", 3) or 3)

        if active_count >= limit:
            raise RuntimeError(
                f"Bot şu an meşgul. Maksimum {limit} eş zamanlı indirme yapılabilir. "
                "Biraz sonra tekrar dene."
            )

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
            # cobalt ayarları worker'a değer olarak geçilir (spawn context'te
            # Config nesnesi paylaşılmaz).
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
        Worker'ı ve TÜM alt süreçlerini (özellikle ffmpeg) öldürür.

        Neden grup halinde: worker'a tek başına SIGTERM göndermek ffmpeg'i
        öksüz bırakıyordu; init'e devrolan ffmpeg diske yazmaya devam ediyor,
        bot onu artık göremiyor ve durduramıyordu. Worker os.setsid() ile
        kendi grubunun lideri olduğu için burada tüm grubu tek seferde
        sonlandırabiliyoruz.
        """
        proc = job.process
        if not proc:
            return

        pid = getattr(proc, "pid", None)

        # 1) Önce nazikçe: tüm gruba SIGTERM
        if pid:
            self._signal_group(pid, signal.SIGTERM)

        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3)
        except Exception:
            pass

        # 2) Direnenler için: tüm gruba SIGKILL
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
        """Worker'ın süreç grubuna sinyal gönderir (öksüz ffmpeg kalmasın)."""
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return

        # Botun kendi grubunu asla öldürme (worker setsid yapamamışsa).
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
        self.jobs.pop(job_id, None)

        return True

    def cancel_user_job(self, user_id: int) -> bool:
        job = self.get_user_active_job(user_id)
        if not job:
            return False
        return self.cancel_job(job.job_id)

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
        Sınırı aşan işleri öldürür ve (job, sebep) listesi döner.

        Üç güvence — hiçbir tek iş botu ya da sunucuyu kilitleyemesin diye:
          1) süre aşımı  → işlem çok uzun sürdü
          2) boyut aşımı → tek iş diski doldurmaya çalışıyor
          3) ölü süreç   → worker çöktü ama slot dolu kaldı

        Bu, canlı yayın tespiti atlatılsa bile çalışan son savunmadır.
        Çağıran: app.queue_consumer (her döngüde, ucuz).
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
                # Süreç öldü ama done/error olayı hiç gelmedi (ör. OOM killer).
                # Slot serbest bırakılmazsa kullanıcı sonsuza dek engellenir.
                #
                # DİKKAT: worker, "done" olayını kuyruğa koyduktan hemen sonra
                # çıkar. Olay henüz tüketilmemişken süreci ölü görüp işi
                # öldürürsek BAŞARILI bir indirmeyi çöpe atarız. Bu yüzden
                # ölüm anını işaretleyip kısa bir süre bekliyoruz; olay bu
                # sürede işlenirse iş zaten jobs'tan düşmüş olur.
                if not job.exited_at:
                    job.exited_at = now
                    continue
                if now - job.exited_at < 10.0:
                    continue
                reason = "dead"

            # Boyut kontrolü diski tarar; her döngüde değil, 15 sn'de bir.
            elif now - job.last_size_check > 15.0:
                job.last_size_check = now
                size = self._dir_size(job.download_dir)
                job.last_size = size
                if size > max_bytes:
                    reason = "oversize"

            if not reason:
                continue

            logger.error(
                "WATCHDOG %s işi sonlandırıldı | sebep=%s | süre=%.0fs | boyut=%.1fMB | url=%s",
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
        # Tamamlanan process'i join ederek zombi/semaphore birikimini önle
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
        self.cleanup_job_files(job_id)

    def detach_job(self, job_id: str) -> None:
        # Başarılı gönderimde iş kaydını kaldır ama dosyaları silme.
        # Dosyalar cache disk-fallback'i ve günlük temizlik için diskte kalır.
        job = self.jobs.pop(job_id, None)
        if not job:
            return
        self.active_by_user.pop(job.user_id, None)
        self._join_process(job)

    def shutdown(self) -> None:
        # /dur, refresh, owner toggle bunu çağırır: yalnızca aktif işleri iptal et.
        # Queue'yu kapatma — bot çalışmaya devam ediyor (aksi halde /basla sonrası bozulur).
        for job_id in list(self.jobs.keys()):
            self.cancel_job(job_id)

    def close(self) -> None:
        # Yalnızca uygulama tamamen kapanırken (post_shutdown) çağrılır.
        # mp.Queue'yu düzgün kapat → sızdırılan semaphore uyarılarını azalt
        self.shutdown()
        try:
            self.queue.close()
            self.queue.cancel_join_thread()
        except Exception:
            pass
