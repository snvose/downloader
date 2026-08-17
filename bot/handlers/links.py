from __future__ import annotations

"""
Gelen medya linklerini işler: YouTube playlist tarayıcısı, YouTube format
menüsü, YouTube Music/Spotify ses indirme ve diğer platformlar için direkt
indirme.
"""

import asyncio
import html
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.safe_message import safe_message_edit, safe_reply
from bot.emoji_manager import em
from bot.i18n import t
from bot.live_guard import format_duration, guard_message, info_is_live, probe_is_live
from bot.pending import clear_user_pending
from bot.ui import analyzing_text, unsupported_spotify_text
from bot.utils import (
    extract_first_url,
    is_spotify_url,
    is_supported_url,
    normalize_url,
    platform_name,
)

logger = logging.getLogger("downloader")

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ─── Yardımcı: güvenli HTML kaçış ─────────────────────────────────────────────

def _esc(v: object) -> str:
    return html.escape(str(v or ""))


# ─── Platform / URL tespiti ────────────────────────────────────────────────────

def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(x in host for x in ["youtube.com", "youtu.be", "music.youtube"])


def _is_youtube_music_url(url: str) -> bool:
    return "music.youtube" in (urlparse(url).netloc or "").lower()


def _is_playlist_url(url: str) -> bool:
    """Yalnızca YouTube playlist URL'si mi?"""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if not any(x in host for x in ["youtube.com", "youtu.be"]):
            return False
        qs = parse_qs(parsed.query)
        if "/playlist" in parsed.path.lower():
            return True
        # list= var ama tek video değil
        return bool(qs.get("list")) and not bool(qs.get("v"))
    except Exception:
        return False


def _human_dur(secs: int | None) -> str:
    if not secs:
        return t("unknown")
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ─── Senkron metadata çekme (executor'da çalıştırılır) ────────────────────────

def _sync_extract_info(url: str, cookies_file: Path | None = None) -> dict:
    """
    İndirme yapmadan metadata çeker — process=True (track/artist/album için).
    Birden fazla deneme: cookie'li → cookiesiz.
    """
    plat = platform_name(url)
    noplaylist = plat not in {"Instagram", "TikTok", "Reddit", "Pinterest"}

    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": noplaylist,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": _HTTP_HEADERS.copy(),
    }

    attempts = [True, False]  # cookie kullan / kullanma
    last_exc: Exception | None = None

    for use_cookies in attempts:
        opts = dict(base_opts)
        if use_cookies and cookies_file and cookies_file.exists():
            opts["cookiefile"] = str(cookies_file)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=True)
            if info is None:
                raise RuntimeError("İçerik bilgisi alınamadı.")
            return _compact_metadata(info, url)
        except Exception as exc:
            last_exc = exc

    raise last_exc or RuntimeError("İçerik bilgisi alınamadı.")


def _sync_extract_playlist(url: str, cookies_file: Path | None = None) -> dict:
    """
    Playlist bilgilerini hızlıca çeker (process=False, extract_flat).
    Ham yt-dlp info dict'ini döner.
    """
    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": _HTTP_HEADERS.copy(),
    }

    last_exc: Exception | None = None
    for use_cookies in [True, False]:
        opts = dict(base_opts)
        if use_cookies and cookies_file and cookies_file.exists():
            opts["cookiefile"] = str(cookies_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            if info is None:
                raise RuntimeError("Playlist bilgisi alınamadı.")
            return info  # type: ignore[return-value]
        except Exception as exc:
            last_exc = exc

    raise last_exc or RuntimeError("Playlist bilgisi alınamadı.")


def _compact_metadata(info: dict, url: str) -> dict:
    """Ham yt-dlp info'yu hafif bir dict'e dönüştürür."""
    if not isinstance(info, dict):
        return {"platform": platform_name(url), "webpage_url": url, "title": ""}

    webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)

    # Platform tespiti — YouTube Music için ekstra kontrol
    plat = platform_name(webpage_url)
    if plat == "YouTube":
        extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
        if "music.youtube" in webpage_url:
            plat = "YouTube Music"
        elif "youtube" in extractor and (
            info.get("track") or info.get("artist") or info.get("album")
        ):
            plat = "YouTube Music"

    description = info.get("description") or info.get("alt_title") or ""
    if isinstance(description, list):
        description = "\n".join(str(x) for x in description if x)

    artist = info.get("artist") or ""
    if isinstance(artist, list):
        artist = ", ".join(str(x) for x in artist if x)

    return {
        "platform": plat,
        # Canlı yayın bayrağı: metadata zaten çekildiği için ek maliyeti yok.
        "is_live": info_is_live(info),
        "title": str(info.get("title") or info.get("fulltitle") or ""),
        "track": str(info.get("track") or ""),
        "artist": str(artist),
        "album": str(info.get("album") or ""),
        "release_year": info.get("release_year"),
        "description": str(description),
        "uploader": str(
            info.get("uploader") or info.get("channel") or info.get("creator") or ""
        ),
        "uploader_id": str(info.get("uploader_id") or info.get("channel_id") or ""),
        "duration": info.get("duration"),
        "webpage_url": webpage_url,
        "thumbnail": str(info.get("thumbnail") or ""),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
    }


# ─── YouTube UI ───────────────────────────────────────────────────────────────

def build_youtube_action_keyboard(job_id: str, view: str = "main") -> InlineKeyboardMarkup:
    """YouTube format seçim klavyesi. view: main | video | audio"""
    back = InlineKeyboardButton("‹ Geri", callback_data=f"menujob|{job_id}|main")

    if view == "video":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✨ En iyi",  callback_data=f"do|{job_id}|video_best"),
                InlineKeyboardButton("1080p",       callback_data=f"do|{job_id}|video_1080"),
            ],
            [
                InlineKeyboardButton("720p",        callback_data=f"do|{job_id}|video_720"),
                InlineKeyboardButton("480p",        callback_data=f"do|{job_id}|video_480"),
                InlineKeyboardButton("360p",        callback_data=f"do|{job_id}|video_360"),
            ],
            # Altyazı videoya gömülür (ayrı .srt dosyası gönderilmez).
            [InlineKeyboardButton("🔤 Altyazılı (TR)", callback_data=f"do|{job_id}|video_720|tr")],
            [InlineKeyboardButton("🔤 Altyazılı (EN)", callback_data=f"do|{job_id}|video_720|en")],
            [back],
        ])

    if view == "audio":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✨ En iyi",  callback_data=f"do|{job_id}|audio_best"),
                InlineKeyboardButton("320k",        callback_data=f"do|{job_id}|audio_320"),
            ],
            [
                InlineKeyboardButton("192k",        callback_data=f"do|{job_id}|audio_192"),
                InlineKeyboardButton("128k",        callback_data=f"do|{job_id}|audio_128"),
            ],
            # FLAC kayıpsızdır ama kaynak kayıplıysa kaliteyi ARTIRMAZ, yalnızca
            # dosyayı büyütür. Bu yüzden ayrı bir seçenek, varsayılan değil.
            [InlineKeyboardButton("🎼 FLAC (kayıpsız)", callback_data=f"do|{job_id}|audio_flac")],
            [back],
        ])

    # main view
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"menujob|{job_id}|video"),
            InlineKeyboardButton("🎧 Ses",   callback_data=f"menujob|{job_id}|audio"),
        ],
        [InlineKeyboardButton("🖼 Kapak",   callback_data=f"do|{job_id}|thumbnail")],
    ])


def build_youtube_info_caption(info: dict, job_id: str) -> str:
    """YouTube / YouTube Music bilgi ekranı metni."""
    plat = info.get("platform", "YouTube")

    if plat == "YouTube Music":
        track  = info.get("track")  or info.get("title") or t("unknown")
        artist = info.get("artist") or info.get("uploader") or t("unknown")
        album  = info.get("album")  or "—"
        year   = info.get("release_year") or "—"
        dur    = _human_dur(info.get("duration"))
        return (
            f"🎵 <b>YouTube Music</b>\n"
            f"┌ 🎼 <b>{_esc(str(track)[:160])}</b>\n"
            f"├ 👤 {_esc(str(artist)[:100])}\n"
            f"├ 💿 {_esc(str(album)[:100])}\n"
            f"├ 📅 {_esc(str(year))}\n"
            f"└ ⏱ {_esc(dur)}"
        )

    # YouTube
    title    = info.get("title")    or t("unknown")
    uploader = info.get("uploader") or info.get("channel") or t("unknown")
    dur      = _human_dur(info.get("duration"))
    lines = [
        "▶️ <b>YouTube</b>",
        f"┌ 🎬 <b>{_esc(str(title)[:180])}</b>",
        f"├ 👤 {_esc(str(uploader)[:100])}",
        f"├ ⏱ {_esc(dur)}",
    ]
    if info.get("view_count"):
        lines.append(f"├ 👁 {int(info['view_count']):,} izlenme".replace(",", "."))
    if info.get("like_count"):
        lines.append(f"├ 👍 {int(info['like_count']):,}".replace(",", "."))

    # Kısa açıklama varsa expandable ekle
    desc = str(info.get("description") or "").strip()
    if desc:
        short = html.escape(desc[:800])
        lines.append(f"├ 📝 <blockquote expandable>{short}</blockquote>")

    lines.append("└ 📥 <i>Format seçiniz</i>")
    return "\n".join(lines)


# ─── Playlist UI ──────────────────────────────────────────────────────────────

def _build_playlist_type_keyboard(pjob_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"pl_type|{pjob_id}|video"),
            InlineKeyboardButton("🎵 Ses",   callback_data=f"pl_type|{pjob_id}|audio"),
        ],
    ])


def build_playlist_track_keyboard(pjob: dict, pjob_id: str, page_size: int = 8) -> InlineKeyboardMarkup:
    entries  = pjob["entries"]
    page     = pjob.get("page", 0)
    selected = pjob.get("selected", set())
    mode     = pjob.get("type", "audio")
    total    = len(entries)
    pages    = max(1, (total + page_size - 1) // page_size)
    start    = page * page_size
    end      = min(start + page_size, total)

    rows: list[list[InlineKeyboardButton]] = []

    for i in range(start, end):
        e   = entries[i]
        raw = str(e.get("title") or e.get("id") or f"#{i+1}")
        lbl = f"{i+1}. {raw[:40]}"
        dur = e.get("duration")
        if dur:
            lbl += f" {_human_dur(int(dur))}"
        chk = "✅" if i in selected else "⬜"
        rows.append([
            InlineKeyboardButton(lbl, callback_data=f"pl_item|{pjob_id}|{i}"),
            InlineKeyboardButton(chk, callback_data=f"pl_sel|{pjob_id}|{i}"),
        ])

    # Sayfa navigasyonu
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pl_page|{pjob_id}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"pl_noop|{pjob_id}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pl_page|{pjob_id}|{page+1}"))
    rows.append(nav)

    # İndirme satırı
    action: list[InlineKeyboardButton] = []
    if selected:
        action.append(InlineKeyboardButton(
            f"⬇️ Seçilenleri İndir ({len(selected)})",
            callback_data=f"pl_dlsel|{pjob_id}",
        ))
    action.append(InlineKeyboardButton("⏩ Hepsini İndir", callback_data=f"pl_dlall|{pjob_id}"))
    rows.append(action)

    # Mod değiştir / iptal
    switch_lbl = "🎬 Video'ya Geç" if mode == "audio" else "🎵 Ses'e Geç"
    switch_typ = "video"           if mode == "audio" else "audio"
    rows.append([InlineKeyboardButton(switch_lbl, callback_data=f"pl_type|{pjob_id}|{switch_typ}")])
    rows.append([InlineKeyboardButton("🛑 İptal",  callback_data=f"pl_cancel|{pjob_id}")])

    return InlineKeyboardMarkup(rows)


def build_playlist_header(pjob: dict) -> str:
    info  = pjob["info"]
    plat  = pjob.get("platform", "YouTube")
    title = str(info.get("title") or info.get("playlist_title") or "Playlist")
    upl   = str(info.get("uploader") or info.get("channel") or "")
    count = len(pjob["entries"])
    icon  = "🎵" if plat == "YouTube Music" else "📁"
    typ   = pjob.get("type")

    lines = [f"{icon} <b>{_esc(title[:80])}</b>"]
    if upl:
        lines.append(f"• <b>Kanal:</b> {_esc(upl[:60])}")
    lines.append(f"• <b>Toplam:</b> {count} içerik")
    if typ:
        lines.append(f"• <b>Mod:</b> {'🎬 Video' if typ == 'video' else '🎵 Ses'}")

    sel = pjob.get("selected", set())
    if sel:
        lines.append(f"✔️ <i>{len(sel)} seçili</i>")

    page  = pjob.get("page", 0)
    pages = max(1, (len(pjob["entries"]) + 8 - 1) // 8)
    lines.append(f"<code>Sayfa {page+1}/{pages}</code>")
    return "\n".join(lines)


# ─── Pending job yardımcıları ─────────────────────────────────────────────────

def _get_user_pending_job(bot_data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    """Kullanıcının format seçim menüsünde bekleyen işini döner (job_id, job) ya da (None, None)."""
    for jid, job in bot_data.get("pending_jobs", {}).items():
        if job.get("user_id") == user_id:
            return jid, job
    return None, None


def _cancel_user_pending(bot_data: dict, user_id: int) -> dict | None:
    """Kullanıcının bekleyen işini iptal eder ve döner (mesaj silmek için)."""
    jid, job = _get_user_pending_job(bot_data, user_id)
    if jid:
        bot_data["pending_jobs"].pop(jid, None)
        return job
    return None


def _get_user_playlist_session(bot_data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    for pid, pjob in bot_data.get("playlist_sessions", {}).items():
        if pjob.get("user_id") == user_id:
            return pid, pjob
    return None, None


# ─── Akış yardımcıları ────────────────────────────────────────────────────────

async def _show_youtube_preview(
    *,
    app,
    wait_msg,
    job_id: str,
    info: dict,
) -> None:
    """
    YouTube bilgi ekranını gönderir:
    Önce thumbnail fotoğrafı dener (daha zengin görünüm),
    başarısız olursa wait_msg'i text olarak düzenler.
    """
    caption  = build_youtube_info_caption(info, job_id)
    keyboard = build_youtube_action_keyboard(job_id, view="main")
    thumb    = info.get("thumbnail")

    if thumb:
        try:
            sent = await app.bot.send_photo(
                chat_id=wait_msg.chat_id,
                photo=thumb,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
                reply_to_message_id=wait_msg.reply_to_message.message_id
                    if wait_msg.reply_to_message else None,
            )
            # Eski "analiz ediliyor" mesajını sil
            try:
                await wait_msg.delete()
            except Exception:
                pass
            return sent
        except Exception:
            pass  # Thumbnail çalışmadı, text fallback

    await safe_message_edit(
        wait_msg,
        caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return wait_msg


async def _run_playlist_download(
    context,
    pjob_id: str,
    indices: list[int],
    status_msg,
) -> None:
    """Playlist'teki seçili parçaları sırayla indirir. Dosyalar her item için
    queue_consumer tarafından gönderilir."""
    bot_data = context.application.bot_data
    pjob     = bot_data.get("playlist_sessions", {}).get(pjob_id)
    if not pjob:
        return

    config  = bot_data["config"]
    manager = bot_data["process_manager"]
    mode_dl = "video_best" if pjob.get("type") == "video" else "audio_best"
    total   = len(indices)
    failed: list[str] = []
    loop = asyncio.get_running_loop()

    # Playlist indirmesi sırasında kullanıcı yeni iş başlatmasın
    # process_manager'a kayıt etmiyoruz (playlist kendi sıralamasını yönetir),
    # bunun yerine playlist_sessions'a "downloading" flag koyuyoruz.
    pjob["downloading"] = True

    for num, idx in enumerate(indices, start=1):
        if pjob.get("cancelled"):
            break

        entry  = pjob["entries"][idx]
        eurl   = (
            entry.get("webpage_url")
            or entry.get("url")
            or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else "")
        )
        etitle = str(entry.get("title") or entry.get("id") or f"#{idx+1}")[:60]

        if not eurl:
            failed.append(f"{idx+1}. URL yok")
            continue

        try:
            await safe_message_edit(
                status_msg,
                f"⬇️ <b>{num}/{total}</b> indiriliyor...\n<i>{_esc(etitle)}</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Metadata çek
        try:
            einfo = await loop.run_in_executor(
                None, _sync_extract_info, eurl, config.cookies_file
            )
        except Exception:
            einfo = {"platform": pjob["platform"], "title": etitle, "webpage_url": eurl, "description": ""}

        # İndir
        try:
            job = manager.start_download(
                user_id=pjob["user_id"],
                chat_id=pjob["chat_id"],
                thread_id=pjob["thread_id"],
                reply_to_message_id=pjob["reply_to"],
                url=eurl,
                mode=mode_dl,
            )
            # NOT: Paylaşılan status mesajını item job'una attach ETMİYORUZ.
            # queue_consumer iş bitince status_message_id'yi siler; bu da
            # playlist'in ortak ilerleme mesajını yok ederdi. Dosyalar yine
            # queue_consumer tarafından gönderilir, sadece silme tetiklenmez.

            # Process tamamlanana dek bekle
            while True:
                await asyncio.sleep(0.5)
                current = manager.jobs.get(job.job_id)
                if not current or current.done or current.cancelled:
                    break

            # Dosyaları al (process zaten upload yaptı, bu blok sadece hata kontrolü)
            if current and current.cancelled:
                failed.append(f"{idx+1}. {etitle}: iptal edildi")

        except Exception as exc:
            failed.append(f"{idx+1}. {_esc(etitle)}: {str(exc)[:80]}")
            logger.warning("Playlist item hatası idx=%d: %s", idx, exc)

    pjob["downloading"] = False
    bot_data.get("playlist_sessions", {}).pop(pjob_id, None)

    if failed:
        fail_lines = "\n".join(failed[:10])
        try:
            await safe_message_edit(
                status_msg,
                f"✅ Tamamlandı ({total - len(failed)}/{total})\n\n"
                f"❌ Başarısız:\n{_esc(fail_lines)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─── Ana handler ──────────────────────────────────────────────────────────────

async def _reject_live(context, *, user, wait_msg, is_admin: bool) -> None:
    """
    Canlı yayın reddini kullanıcıya bildirir ve denemeyi kaydeder.

    1./2. deneme → uyarı, 3. deneme → geçici ban. Admin sayaca dahil değildir.
    """
    guard = context.application.bot_data.get("live_guard")
    text = t("live_not_supported")

    if guard and not is_admin:
        result = await asyncio.get_running_loop().run_in_executor(
            None, guard.register_attempt, user.id
        )
        text = guard_message(result)

    logger.warning("Canlı yayın reddedildi | user=%s", user.id)

    await safe_message_edit(
        wait_msg,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _try_serve_from_cache(
    context,
    *,
    url: str,
    mode: str,
    chat,
    message,
    silent: bool,
) -> bool:
    """
    Aynı link daha önce indirilmişse yeniden indirme yapmadan iletir.
    Dönüş True ise istek cache'den karşılandı (yeni indirme gereksiz).
    """
    bot_data = context.application.bot_data
    cache = bot_data.get("media_cache")
    if not cache:
        return False

    # Blocking JSON okuması → thread
    record = await asyncio.get_running_loop().run_in_executor(
        None, cache.resolve_sendable, url, mode
    )
    if not record:
        return False

    from bot.sender import send_from_cache  # geç import (döngüsel bağımlılık)

    try:
        result_items = await send_from_cache(
            context=context.application,
            chat_id=chat.id,
            thread_id=message.message_thread_id,
            reply_to_message_id=message.message_id,
            record=record,
            source_url=url,
            bare=silent,
        )
    except Exception as exc:
        logger.warning("Cache'den gönderim başarısız, normal indirmeye düşülüyor: %s", exc)
        return False

    # file_id güncelle (disk→yükle senaryosunda yeni file_id'ler oluşur)
    await asyncio.get_running_loop().run_in_executor(
        None, cache.update_file_ids, url, mode, result_items
    )

    # Kullanım kaydını güncelle
    registry = bot_data.get("chat_registry")
    if registry:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: registry.record_download(
                chat_id=chat.id,
                title=getattr(chat, "title", None) or "",
                chat_type=chat.type,
                platform=platform_name(url),
            ),
        )
    return True


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message:
        return

    user    = update.effective_user
    message = update.message
    chat    = update.effective_chat

    # ── İzin kontrolü (rate limit dahil) ──────────────────────────────────────
    permissions = context.application.bot_data["permissions"]
    check = permissions.check_update(update)
    if not check.allowed:
        if chat.type == "private":
            await safe_reply(message, check.reason)
        return

    # ── URL çıkar ─────────────────────────────────────────────────────────────
    raw = message.text or message.caption or ""
    url = normalize_url(extract_first_url(raw) or "")
    if not url or not is_supported_url(url):
        return

    # ── Kullanıcı/sohbet kaydı (duyuru listesi + istatistik) ──────────────────
    # Desteklenen bir link gönderen herkes kaydedilir; indirme başarısız olsa
    # bile kullanıcı bilinir hale gelir (Faz 3 duyuru hedefi).
    # Yazma tamponu: her mesajda diske yazmak yerine bellekte biriktirilip
    # periyodik olarak tek transaction'da yazılır (bkz. bot/analytics.py).
    activity = context.application.bot_data.get("activity_buffer")
    if activity:
        try:
            await activity.touch_user(
                user.id,
                username=user.username,
                first_name=user.first_name,
                language=user.language_code,
            )
            await activity.touch_chat(
                chat.id,
                title=getattr(chat, "title", None) or "",
                chat_type=chat.type,
            )
        except Exception:
            logger.exception("Aktivite kaydı başarısız")

    # ── Geçici ban (canlı yayın spamı) ────────────────────────────────────────
    # İzin kontrolünden sonra, iş başlatılmadan önce. Süresi dolan ban
    # ban_remaining() içinde kendiliğinden kalkar.
    live_guard = context.application.bot_data.get("live_guard")
    is_admin = permissions.is_admin(user.id)

    if live_guard and not is_admin:
        remaining = await asyncio.get_running_loop().run_in_executor(
            None, live_guard.ban_remaining, user.id
        )
        if remaining > 0:
            if chat.type == "private":
                await safe_reply(
                    message,
                    t("live_ban_active", duration=format_duration(remaining)),
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            return

    bot_data = context.application.bot_data
    manager  = bot_data["process_manager"]
    config   = bot_data["config"]
    loop     = asyncio.get_running_loop()
    state    = bot_data["bot_state"]

    # ── Bakım modu: hiçbir indirme yapılmaz, sabit mesaj döner ─────────────────
    # Admin için bakım modu uygulanmaz (admin test edebilsin).
    if state.is_maintenance() and not permissions.is_admin(user.id):
        await safe_reply(message, t("maintenance"), reply_to_message_id=message.message_id)
        return

    # ── Safe mode: sessiz çalışma. Kullanıcıya mesaj/yazıyor/emoji yok ─────────
    silent = state.is_safe()

    # ── Cache: aynı link daha önce indirildiyse yeniden indirme ────────────────
    # Doğrudan akışlar (TikTok/Instagram/Spotify/YT Music vb.) cache'den karşılanır.
    # YouTube format menüsü interaktif seçim gerektirdiği için cache'lenmez.
    if not _is_youtube_url(url) or _is_youtube_music_url(url):
        cache_mode = "audio_best" if (is_spotify_url(url) or _is_youtube_music_url(url)) else "auto"
        if not manager.get_user_active_job(user.id):
            served = await _try_serve_from_cache(
                context, url=url, mode=cache_mode,
                chat=chat, message=message, silent=silent,
            )
            if served:
                return

    # ── Safe mode akışı: sessizce indir, yalnızca medyayı yanıt olarak ilet ────
    if silent:
        if manager.get_user_active_job(user.id):
            return  # safe modda bekleme bildirimi bile gönderilmez
        safe_mode_dl = "audio_best" if (is_spotify_url(url) or _is_youtube_music_url(url)) else "auto"
        try:
            manager.start_download(
                user_id=user.id,
                chat_id=chat.id,
                thread_id=message.message_thread_id,
                reply_to_message_id=message.message_id,
                url=url,
                mode=safe_mode_dl,
                silent=True,
                username=user.username,
                chat_title=getattr(chat, "title", None),
                chat_type=chat.type,
            )
            # status mesajı YOK — safe mode tamamen sessiz
        except Exception:
            logger.exception("Safe mode indirme başlatılamadı | url=%s", url)
        return

    # ── Platform notu ──────────────────────────────────────────────────────────
    # Facebook sosyal platform olarak indirilir.
    # Spotify: yt-dlp doğrudan indiremez; worker şarkıyı YouTube'da bulup ses
    # olarak indirir (aşağıda Spotify dalında ele alınır).

    # ── Aktif process işi kontrolü ────────────────────────────────────────────
    if manager.get_user_active_job(user.id):
        await safe_reply(
            message,
            t("wait_active"),
            reply_to_message_id=message.message_id,
        )
        return

    # ── Bekleyen format menüsü iptal et (yeni link geldi) ─────────────────────
    # Eski menü mesajı SİLİNİR; önceden yalnızca butonları kaldırılıyor ve
    # mesaj sohbette kalıntı olarak duruyordu.
    await clear_user_pending(context.application, user.id)

    # ── "Analiz ediliyor" mesajı ───────────────────────────────────────────────
    wait_msg = await safe_reply(
        message,
        analyzing_text(url),
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
        disable_web_page_preview=True,
    )
    if not wait_msg:
        return

    plat = platform_name(url)

    try:
        # ── 0. Spotify → şarkıyı YouTube üzerinden ses olarak indir ─────────────
        if is_spotify_url(url):
            if "/track/" not in (url.lower()):
                await safe_message_edit(
                    wait_msg,
                    unsupported_spotify_text(),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            try:
                job = manager.start_download(
                    user_id=user.id,
                    chat_id=chat.id,
                    thread_id=message.message_thread_id,
                    reply_to_message_id=message.message_id,
                    url=url,
                    mode="audio_best",
                    username=user.username,
                    chat_title=getattr(chat, "title", None),
                    chat_type=chat.type,
                )
                manager.attach_status_message(job.job_id, wait_msg.message_id)
                await safe_message_edit(
                    wait_msg,
                    f"{em('icon_spotify')} <i>Spotify şarkısı hazırlanıyor...</i>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                await safe_message_edit(wait_msg, t("job_start_failed"))
            return

        # ── 1. YouTube Playlist ────────────────────────────────────────────────
        if _is_playlist_url(url) and plat in {"YouTube", "YouTube Music"}:
            if chat.type in {"group", "supergroup"}:
                try:
                    me = await context.bot.get_me()
                    bot_url = f"https://t.me/{me.username}"
                except Exception:
                    bot_url = "https://t.me"
                await safe_message_edit(
                    wait_msg,
                    "Playlist indirme gruplarda kapalı. Playlist için botla özelden sohbet aç.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Botu özelden aç", url=bot_url)]
                    ]),
                )
                return

            # Playlist metadata çek
            try:
                raw_info = await loop.run_in_executor(
                    None, _sync_extract_playlist, url, config.cookies_file
                )
            except Exception as exc:
                await safe_message_edit(
                    wait_msg,
                    f"❌ Playlist bilgisi alınamadı: {_esc(str(exc)[:200])}",
                    parse_mode="HTML",
                )
                return

            entries = [e for e in (raw_info.get("entries") or []) if isinstance(e, dict)]

            # Playlist içindeki canlı yayınlar ayıklanır — tek bir canlı girdi
            # tüm playlist indirmesini süresiz askıda bırakırdı.
            live_count = sum(1 for e in entries if info_is_live(e))
            if live_count:
                entries = [e for e in entries if not info_is_live(e)]
                logger.info("Playlist'ten %d canlı yayın ayıklandı", live_count)

            if not entries:
                await safe_message_edit(
                    wait_msg,
                    t("live_not_supported") if live_count
                    else "❌ Bu playlist boş veya erişime kapalı.",
                    parse_mode="HTML" if live_count else None,
                )
                return

            pjob_id = uuid.uuid4().hex[:12]
            # YouTube Music playlist → otomatik ses modu
            auto_type = "audio" if plat == "YouTube Music" else None

            pjob = {
                "pjob_id": pjob_id,
                "url": url,
                "info": raw_info,
                "entries": entries,
                "platform": plat,
                "user_id": user.id,
                "chat_id": chat.id,
                "thread_id": message.message_thread_id,
                "reply_to": message.message_id,
                "created_at": time.time(),
                "page": 0,
                "selected": set(),
                "type": auto_type,
                "cancelled": False,
                "downloading": False,
            }
            bot_data.setdefault("playlist_sessions", {})[pjob_id] = pjob

            text   = build_playlist_header(pjob)
            markup = (
                build_playlist_track_keyboard(pjob, pjob_id)
                if auto_type
                else _build_playlist_type_keyboard(pjob_id)
            )
            await safe_message_edit(
                wait_msg,
                text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return

        # ── 2 & 3. YouTube / YouTube Music ────────────────────────────────────
        if _is_youtube_url(url):
            # Önce hızlı canlı yayın sorgusu (extract_flat, ~1.5 sn), sonra tam
            # metadata. Paralel denendi ama YouTube eşzamanlı iki isteği
            # yavaşlattığı için sıralı akış hem daha hızlı reddediyor hem de
            # canlı yayında pahalı metadata çekimini tamamen atlıyor.
            # Cookie BİLEREK kullanılmıyor: canlı yayın bilgisi herkese açık ve
            # cookie'li sorgu ölçümde 2 kat yavaş (3.6 sn vs 1.5 sn). Sorgu
            # başarısız olursa (yaş kısıtlı/özel içerik) False döner ve akış
            # normal devam eder — worker tarafındaki cookie'li kontrol yakalar.
            try:
                probe_live, _probe_info = await loop.run_in_executor(
                    None, probe_is_live, url
                )
            except Exception:
                probe_live = False

            if probe_live:
                await _reject_live(
                    context, user=user, wait_msg=wait_msg, is_admin=is_admin
                )
                return

            try:
                info = await loop.run_in_executor(
                    None, _sync_extract_info, url, config.cookies_file
                )
            except Exception:
                # Metadata alınamazsa platforma göre fallback yap
                info = {
                    "platform": plat,
                    "title": "",
                    "uploader": "",
                    "description": "",
                    "duration": None,
                    "webpage_url": url,
                    "thumbnail": None,
                }

            # ── Canlı yayın: indirmeyi hiç başlatma ────────────────────────────
            # Metadata yukarıda zaten çekildi; bu kontrol ek gecikme getirmez.
            # Canlı yayın sonsuz akar, iş slotunu süresiz doldurur ve diski
            # doldurana kadar büyür — o yüzden burada kesiliyor.
            if info.get("is_live"):
                await _reject_live(
                    context, user=user, wait_msg=wait_msg, is_admin=is_admin
                )
                return

            detected_plat = info.get("platform", plat)

            # ── YouTube Music → otomatik ses indirme ──────────────────────────
            if detected_plat == "YouTube Music" or _is_youtube_music_url(url):
                info["platform"] = "YouTube Music"
                # Kısa bilgi mesajı göster
                yt_music_text = build_youtube_info_caption(info, "")
                try:
                    await safe_message_edit(
                        wait_msg,
                        yt_music_text + "\n\n⏳ <i>Ses hazırlanıyor...</i>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass

                try:
                    job = manager.start_download(
                        user_id=user.id,
                        chat_id=chat.id,
                        thread_id=message.message_thread_id,
                        reply_to_message_id=message.message_id,
                        url=url,
                        mode="audio_best",
                        username=user.username,
                        chat_title=getattr(chat, "title", None),
                        chat_type=chat.type,
                    )
                    manager.attach_status_message(job.job_id, wait_msg.message_id)
                except Exception as exc:
                    await safe_message_edit(wait_msg, t("job_start_failed"))
                return

            # ── YouTube → format seçim menüsü ─────────────────────────────────
            job_id = uuid.uuid4().hex[:12]

            sent_msg = await _show_youtube_preview(
                app=context.application,
                wait_msg=wait_msg,
                job_id=job_id,
                info=info,
            )

            # Pending job kaydet
            status_message_id = (
                sent_msg.message_id if sent_msg else wait_msg.message_id
            )
            bot_data.setdefault("pending_jobs", {})[job_id] = {
                "job_id": job_id,
                "user_id": user.id,
                "url": url,
                "info": info,
                "chat_id": chat.id,
                "thread_id": message.message_thread_id,
                "reply_to": message.message_id,
                "created_at": time.time(),
                "status_message_id": status_message_id,
                # Detaylı loglama + chat kaydı için kullanıcı bağlamı
                "username": user.username,
                "chat_title": getattr(chat, "title", None),
                "chat_type": chat.type,
            }
            return

        # ── 4. Diğer platformlar → direkt indirme ─────────────────────────────
        try:
            job = manager.start_download(
                user_id=user.id,
                chat_id=chat.id,
                thread_id=message.message_thread_id,
                reply_to_message_id=message.message_id,
                url=url,
                mode="auto",
                username=user.username,
                chat_title=getattr(chat, "title", None),
                chat_type=chat.type,
            )
            manager.attach_status_message(job.job_id, wait_msg.message_id)

            await safe_message_edit(
                wait_msg,
                t("preparing"),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            await safe_message_edit(wait_msg, t("job_start_failed"))

    except Exception:
        logger.exception("link_handler genel hata | user=%s url=%s", user.id, url)
        try:
            await safe_message_edit(
                wait_msg,
                "❌ Beklenmeyen bir hata oluştu. Link bozuk, gizli veya geçici erişim sorunu olabilir.",
            )
        except Exception:
            pass
