from __future__ import annotations

import asyncio
import html
import logging
import shutil
import sys
from datetime import datetime

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot import analytics
from bot.broadcast import BroadcastJob, run_broadcast
from bot.i18n import LANGUAGES, set_language
from bot.live_guard import format_duration
from bot.cookie_health import platform_cookie_status
from bot.pending import clear_all_pending
from bot.safe_message import safe_reply
from bot.state import MODE_MAINTENANCE, MODE_NORMAL, MODE_SAFE


logger = logging.getLogger("downloader")


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _admin_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    permissions = context.application.bot_data["permissions"]
    return permissions.is_admin(update.effective_user.id)


async def dur_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    permissions.set_bot_enabled(False)
    manager.shutdown()

    await safe_reply(update.message, "Bot durduruldu. Aktif işler iptal edildi.")


async def basla_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    permissions = context.application.bot_data["permissions"]
    permissions.set_bot_enabled(True)

    await safe_reply(update.message, "Bot başlatıldı.")


async def banid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await safe_reply(
            update.message,
            "Kullanım: <code>/banid ID</code>\n\n"
            "Pozitif ID → kullanıcı, negatif ID → grup/kanal.",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID sayısal olmalı.")
        return

    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    # ID'nin işaretine göre doğru listeye yazılır. Önceden her ID "users"
    # listesine gidiyordu; grup ID'si oraya yazılınca hiçbir kontrolle
    # eşleşmiyor ve grup banı hiç işlemiyordu.
    if permissions.ban_id(target_id):
        cancelled = manager.cancel_chat_jobs(target_id)
        note = f" ({cancelled} aktif indirme iptal edildi)" if cancelled else ""
        await safe_reply(update.message, f"Grup banlandı: {target_id}{note}")
    else:
        manager.cancel_user_job(target_id)
        await safe_reply(update.message, f"Kullanıcı banlandı: {target_id}")


async def unbanid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await safe_reply(
            update.message,
            "Kullanım: <code>/unbanid ID</code>\n\n"
            "Pozitif ID → kullanıcı, negatif ID → grup/kanal.",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID sayısal olmalı.")
        return

    permissions = context.application.bot_data["permissions"]
    kind = "Grup" if permissions.unban_id(target_id) else "Kullanıcı"

    await safe_reply(update.message, f"{kind} banı kaldırıldı: {target_id}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    config = context.application.bot_data["config"]
    manager = context.application.bot_data["process_manager"]
    permissions = context.application.bot_data["permissions"]

    active_jobs = [
        job for job in manager.jobs.values()
        if not job.done and not job.cancelled
    ]

    counts = permissions.counts()

    ffmpeg = "var" if shutil.which("ffmpeg") else "yok"
    gallery_dl = "var" if shutil.which("gallery-dl") else "yok"

    text = (
        f"<b>{config.bot_name} — Status</b>\n\n"
        f"Bot durumu: <b>{'aktif' if counts['enabled'] else 'durduruldu'}</b>\n"
        f"Local Bot API: <b>{'aktif' if config.local_bot_api_base else 'kapalı'}</b>\n"
        f"Aktif indirme: <b>{len(active_jobs)}</b>\n"
        f"Toplam job kaydı: <b>{len(manager.jobs)}</b>\n"
        f"Max eş zamanlı: <b>{config.max_simultaneous_downloads}</b>\n"
        f"Max dosya: <b>{config.max_file_size_mb} MB</b>\n\n"
        f"Banlı kullanıcı: <b>{counts['banned_users']}</b>\n"
        f"Banlı grup: <b>{counts['banned_groups']}</b>\n\n"
        f"Python: <code>{sys.version.split()[0]}</code>\n"
        f"yt-dlp: <code>{yt_dlp.version.__version__}</code>\n"
        f"ffmpeg: <b>{ffmpeg}</b>\n"
        f"gallery-dl: <b>{gallery_dl}</b>\n\n"
        f"Download dizini:\n<code>{config.download_dir}</code>"
    )

    await safe_reply(update.message, 
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return
    manager = context.application.bot_data["process_manager"]
    manager.shutdown()
    # Menü mesajlarını da sil: aksi halde ekranda tıklanabilir ama karşılığı
    # olmayan öksüz format menüleri kalıyordu.
    cleared = await clear_all_pending(context.application)
    context.application.bot_data["playlist_sessions"] = {}
    await safe_reply(update.message, f"Tüm işler temizlendi. ({cleared} menü kaldırıldı)")


# ── /admin paneli ────────────────────────────────────────────────────────────

_MODE_LABEL = {
    MODE_NORMAL: "🟢 Normal",
    MODE_SAFE: "🔇 Safe Mode (sessiz)",
    MODE_MAINTENANCE: "🛠 Bakım Modu",
}

# Mod satırı butonları için kısa etiketler
_MODE_SHORT = {
    MODE_NORMAL: "🟢 Normal",
    MODE_SAFE: "🔇 Safe",
    MODE_MAINTENANCE: "🛠 Bakım",
}


async def _edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    """
    Panel mesajını günceller.

    Önceden tüm istisnalar sessizce yutuluyordu: panel güncellenmezse admin
    hiçbir geri bildirim almıyor, butona bastığında hiçbir şey olmuyordu.
    Artık "değişiklik yok" hatası (zararsız) ayrılıyor, gerçek hatalar
    loglanıyor ve kullanıcıya uyarı gösteriliyor.
    """
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True,
        )
    except Exception as exc:
        message = str(exc).lower()
        # Aynı içerik tekrar gönderilince Telegram hata döndürür; bu normal.
        if "not modified" in message:
            return
        logger.warning("Admin paneli güncellenemedi: %s", exc)
        try:
            await query.answer(
                "Panel güncellenemedi, /admin ile yeniden aç.", show_alert=True
            )
        except Exception:
            pass


# ── Ana panel ──
def _panel_keyboard(state) -> InlineKeyboardMarkup:
    mode = state.get_mode()
    enabled = state.get_enabled()
    mode_row = [
        InlineKeyboardButton(
            ("🔘 " if mode == m else "⚪️ ") + _MODE_SHORT[m].split(" ", 1)[1],
            callback_data=f"admin|mode|{m}",
        )
        for m in (MODE_NORMAL, MODE_SAFE, MODE_MAINTENANCE)
    ]
    return InlineKeyboardMarkup([
        mode_row,
        [InlineKeyboardButton(
            "⏸ Botu Durdur" if enabled else "▶️ Botu Başlat",
            callback_data="admin|toggle",
        )],
        [
            InlineKeyboardButton("🌐 Dil", callback_data="admin|langmenu"),
            InlineKeyboardButton("📈 Analitik", callback_data="admin|analytics"),
            InlineKeyboardButton("🖥 Sistem", callback_data="admin|status"),
        ],
        [
            InlineKeyboardButton("💬 Kullanım", callback_data="admin|usage|0"),
            InlineKeyboardButton("🚫 Banlar", callback_data="admin|bans"),
            InlineKeyboardButton("🍪 Cookie", callback_data="admin|cookie"),
        ],
        [
            InlineKeyboardButton("📣 Duyuru", callback_data="admin|broadcast"),
            InlineKeyboardButton("📜 Log", callback_data="admin|logs|live|all"),
            InlineKeyboardButton("🎨 Emoji", callback_data="emoji|page|0"),
        ],
        [
            InlineKeyboardButton("🧹 İşleri Temizle", callback_data="admin|clear"),
        ],
        [
            InlineKeyboardButton("🔄 Yenile", callback_data="admin|panel"),
            InlineKeyboardButton("✖️ Kapat", callback_data="admin|close"),
        ],
    ])


_MODE_HINT = {
    MODE_NORMAL: "olağan çalışma",
    MODE_SAFE: "sessiz — yalnızca medya, mesaj/buton yok",
    MODE_MAINTENANCE: "indirme kapalı — sabit bilgi mesajı",
}


def _panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    state = context.application.bot_data["bot_state"]
    manager = context.application.bot_data["process_manager"]
    config = context.application.bot_data["config"]
    # Sayılar TEK kaynaktan (DB) okunur. Önceden panel chats.json'dan,
    # istatistik ekranı usage_stats.json'dan, analitik DB'den okuyordu —
    # aynı bilgi üç yerde farklı görünebiliyordu.
    db = context.application.bot_data.get("db")
    s = db.stats() if db else {"total_chats": 0, "groups": 0, "privates": 0, "total_downloads": 0}
    mode = state.get_mode()
    lang = state.get_language()
    enabled = state.get_enabled()
    active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
    limit = config.max_simultaneous_downloads
    bar = "▰" * active + "▱" * max(0, limit - active)
    return (
        f"<b>⚙️ {config.bot_name} · Admin Paneli</b>\n"
        "──────────────────\n"
        f"{'🟢' if enabled else '⏸'} Durum: <b>{'Çalışıyor' if enabled else 'Durduruldu'}</b>\n"
        f"{_MODE_LABEL.get(mode, mode).split(' ')[0]} Mod: <b>{_MODE_LABEL.get(mode, mode).split(' ', 1)[1]}</b>\n"
        f"      <i>{_MODE_HINT.get(mode, '')}</i>\n"
        f"🌐 Dil: <b>{LANGUAGES.get(lang, lang)}</b>\n"
        f"⚡ Aktif indirme: <b>{active}/{limit}</b>  <code>{bar}</code>\n"
        "──────────────────\n"
        f"💬 Sohbet: <b>{s['total_chats']}</b> "
        f"(grup <b>{s['groups']}</b> · özel <b>{s['privates']}</b>)\n"
        f"📥 Toplam indirme: <b>{s['total_downloads']}</b>"
    )


# ── Dil menüsü ──
def _language_keyboard(current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, name in LANGUAGES.items():
        mark = "✅ " if code == current else ""
        row.append(InlineKeyboardButton(f"{mark}{name}", callback_data=f"admin|lang|{code}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])
    return InlineKeyboardMarkup(rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("‹ Panel", callback_data="admin|panel")]])


def _cookie_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 Hata Kaydı", callback_data="admin|cookielog"),
            InlineKeyboardButton("🔄 Yenile", callback_data="admin|cookie"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


def _cookie_log_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧹 Sayaçları Sıfırla", callback_data="admin|cookiereset"),
            InlineKeyboardButton("‹ Cookie", callback_data="admin|cookie"),
        ],
    ])


# ── Cookie durumu ──
_COOKIE_STATUS_ICON = {
    "expired": "🔴",
    "missing": "🔴",
    "expiring": "🟠",
    "optional_missing": "⚪️",
    "ok": "🟢",
}

_COOKIE_STATUS_LABEL = {
    "expired": "SÜRESİ DOLMUŞ",
    "missing": "EKSİK",
    "expiring": "yakında bitiyor",
    "optional_missing": "yok (gerekmiyor)",
    "ok": "geçerli",
}


def _cookie_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Platform bazlı cookie durumu.

    Admin'in tek bakışta "hangi cookie'yi yenilemeliyim" sorusunu
    yanıtlaması için: durum + kalan gün + o cookie yüzünden başarısız
    olan istek sayısı.
    """
    config = context.application.bot_data["config"]
    cookie_log = context.application.bot_data.get("cookie_log")
    cf = config.cookies_file

    failures = cookie_log.failures() if cookie_log else {}
    rows = platform_cookie_status(cf, failures=failures)

    lines = ["<b>🍪 Cookie Durumu</b>", ""]

    if not cf.exists():
        lines.append(f"❌ Cookie dosyası yok:\n<code>{cf}</code>\n")
    else:
        try:
            size = cf.stat().st_size
        except OSError:
            size = 0
        from bot.utils import human_bytes
        total = sum(r["count"] for r in rows)
        lines.append(f"📄 <code>{cf.name}</code> · {human_bytes(size)} · {total} çerez")
        lines.append("")

    problems = [r for r in rows if r["status"] in {"expired", "missing", "expiring"}]

    for row in rows:
        icon = _COOKIE_STATUS_ICON.get(row["status"], "⚪️")
        label = _COOKIE_STATUS_LABEL.get(row["status"], row["status"])
        parts = [f"{icon} <b>{row['platform']}</b> — {label}"]

        if row["count"]:
            detail = f"{row['count']} çerez"
            if row["days_left"] is not None:
                if row["days_left"] < 0:
                    detail += " · süresi doldu"
                elif row["days_left"] == 0:
                    detail += " · <b>bugün bitiyor</b>"
                else:
                    detail += f" · {row['days_left']} gün kaldı"
            if row["expired"]:
                detail += f" · {row['expired']} tanesi dolmuş"
            parts.append(f"   <i>{detail}</i>")

        if row["failures"]:
            reason = _esc(row["last_reason"])[:60]
            parts.append(
                f"   ⚠️ <b>{row['failures']}</b> başarısız istek"
                + (f" · son sebep: {reason}" if reason else "")
            )

        lines.append("\n".join(parts))

    lines.append("")
    if problems:
        names = ", ".join(r["platform"] for r in problems)
        lines.append(f"👉 <b>Yenilenmesi gereken:</b> {names}")
    else:
        lines.append("✅ Tüm platformların çerezleri geçerli.")

    total_fail = cookie_log.total() if cookie_log else 0
    if total_fail:
        lines.append(
            f"\n📁 Toplam <b>{total_fail}</b> cookie kaynaklı hata "
            f"— <code>logs/cookie_errors.log</code>"
        )

    return "\n".join(lines)


def _cookie_log_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Son cookie hatalarının ham log satırları."""
    cookie_log = context.application.bot_data.get("cookie_log")
    if not cookie_log:
        return "<b>🍪 Cookie Log</b>\n\nKayıt yok."

    entries = cookie_log.tail(12)
    if not entries:
        return (
            "<b>🍪 Cookie Log</b>\n\n"
            "Henüz cookie kaynaklı bir hata kaydedilmedi. ✅"
        )

    body = "\n\n".join(f"<code>{_esc(line)}</code>" for line in reversed(entries))
    return f"<b>🍪 Cookie Log</b> <i>(son {len(entries)})</i>\n\n{body}"


# ── Analitik dashboard ───────────────────────────────────────────────────────

def _sparkline(values: list[int]) -> str:
    """Küçük metin grafiği — günlük indirme eğilimi."""
    if not values or max(values) == 0:
        return "▁" * len(values)
    blocks = "▁▂▃▄▅▆▇█"
    peak = max(values)
    return "".join(blocks[min(len(blocks) - 1, int(v / peak * (len(blocks) - 1)))] for v in values)


def _bar(value: int, total: int, width: int = 10) -> str:
    if not total:
        return "░" * width
    filled = int(value / total * width)
    return "█" * filled + "░" * (width - filled)


def _analytics_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    if not db:
        return "<b>📈 Analitik</b>\n\nVeritabanı yok."

    data = analytics.summary(db)
    daily = analytics.daily_counts(db, 7)
    platforms = analytics.platform_distribution(db, days=30)
    sources = analytics.source_distribution(db)
    split = data["chat_split"]
    fail = data["failure"]

    counts = [d["count"] for d in daily]
    week_total = sum(counts)

    lines = [
        "<b>📈 Analitik Panosu</b>",
        "",
        "<b>Aktif kullanıcı</b>",
        f"  Bugün: <b>{data['dau']}</b> · Hafta: <b>{data['wau']}</b> · Ay: <b>{data['mau']}</b>",
        "",
        "<b>İndirme</b>",
        f"  Bugün: <b>{data['downloads_today']}</b> · "
        f"7 gün: <b>{week_total}</b> · Toplam: <b>{data['total_downloads']}</b>",
        f"  <code>{_sparkline(counts)}</code> <i>son 7 gün</i>",
        "",
        "<b>Başarı oranı (7 gün)</b>",
        f"  <code>{_bar(fail['ok'], fail['total'])}</code> "
        f"<b>%{fail['rate']:.0f}</b> ({fail['ok']}/{fail['total']})",
        "",
        "<b>Sohbet dağılımı</b>",
        f"  👤 Özel: <b>{split['private']}</b> · 👥 Grup: <b>{split['group']}</b>",
    ]

    if platforms:
        total = sum(int(p["count"]) for p in platforms) or 1
        lines.append("")
        lines.append("<b>Platform dağılımı (30 gün)</b>")
        for row in platforms[:6]:
            count = int(row["count"])
            lines.append(
                f"  {_esc(row['platform']):<14} <code>{_bar(count, total, 8)}</code> "
                f"<b>{count}</b> <i>(%{count * 100 // total})</i>"
            )

    if sources:
        parts = " · ".join(f"{_esc(s['source'])}: <b>{s['count']}</b>" for s in sources)
        lines.append("")
        lines.append(f"<b>İndirme kaynağı</b>\n  {parts}")

    buffer = context.application.bot_data.get("activity_buffer")
    if buffer and buffer.pending():
        lines.append(f"\n<i>({buffer.pending()} aktivite kaydı yazılmayı bekliyor)</i>")

    return "\n".join(lines)


def _top_users_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    if not db:
        return "<b>🏆 En Aktif Kullanıcılar</b>\n\nVeritabanı yok."

    rows = analytics.top_users(db, 15)
    if not rows:
        return "<b>🏆 En Aktif Kullanıcılar</b>\n\nHenüz kayıt yok."

    lines = ["<b>🏆 En Aktif Kullanıcılar</b>", ""]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}

    for index, row in enumerate(rows):
        mark = medals.get(index, f"{index + 1}.")
        name = row.get("username")
        label = f"@{_esc(name)}" if name else _esc(row.get("first_name") or "—")
        last = row.get("last_activity")
        when = datetime.fromtimestamp(last).strftime("%d.%m") if last else "-"
        lines.append(
            f"{mark} {label} — <b>{row['total_downloads']}</b> indirme "
            f"<i>({when})</i>\n     <code>{row['user_id']}</code>"
        )

    return "\n".join(lines)


def _analytics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 En Aktif", callback_data="admin|topusers"),
            InlineKeyboardButton("🔄 Yenile", callback_data="admin|analytics"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


# ── Duyuru (broadcast) ───────────────────────────────────────────────────────

_BC_KIND_LABEL = {
    "all": "herkes (kullanıcı + grup)",
    "users": "yalnızca özel sohbetler",
    "groups": "yalnızca gruplar",
}


def _bc_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Admin'in duyuru taslağı (tek admin olduğu için uygulama genelinde tek)."""
    return context.application.bot_data.setdefault("broadcast_compose", {})


def _broadcast_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    draft = _bc_state(context)
    kind = draft.get("kind", "all")

    counts = {"all": 0, "users": 0, "groups": 0}
    if db:
        try:
            counts = {k: len(db.broadcast_targets(kind=k)) for k in counts}
        except Exception:
            logger.exception("Duyuru hedefleri okunamadı")

    lines = [
        "<b>📣 Duyuru Gönder</b>",
        "",
        f"🎯 Hedef kitle: <b>{_BC_KIND_LABEL.get(kind, kind)}</b>",
        f"👥 Ulaşılacak: <b>{counts.get(kind, 0)}</b> sohbet",
        "",
        f"<i>Toplam: {counts['users']} özel · {counts['groups']} grup "
        "(duyuru kapatanlar ve engelleyenler hariç)</i>",
        "",
    ]

    message = draft.get("text")
    if message:
        preview = _esc(message)
        if len(preview) > 600:
            preview = preview[:600] + "…"
        lines.append("<b>📝 Mesaj önizleme</b>")
        lines.append(f"<blockquote>{preview}</blockquote>")
        lines.append("")
        lines.append("Göndermeye hazır. ⬇️")
    else:
        lines.append(
            "✍️ <b>Mesaj yok.</b>\n"
            "<i>Aşağıdaki butona basıp duyuru metnini bana gönder. "
            "HTML biçimlendirme (kalın, italik, link) kullanabilirsin.</i>"
        )

    return "\n".join(lines)


def _broadcast_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    draft = _bc_state(context)
    kind = draft.get("kind", "all")
    has_text = bool(draft.get("text"))

    kind_row = [
        InlineKeyboardButton(
            ("🔘 " if kind == k else "⚪️ ") + label,
            callback_data=f"admin|bckind|{k}",
        )
        for k, label in (("all", "Herkes"), ("users", "Özel"), ("groups", "Grup"))
    ]

    rows = [kind_row]

    if has_text:
        rows.append([InlineKeyboardButton("🚀 Gönder", callback_data="admin|bcconfirm")])
        rows.append([
            InlineKeyboardButton("✏️ Mesajı Değiştir", callback_data="admin|bcwrite"),
            InlineKeyboardButton("🗑 Mesajı Sil", callback_data="admin|bcclear"),
        ])
    else:
        rows.append([InlineKeyboardButton("✍️ Mesaj Yaz", callback_data="admin|bcwrite")])

    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])
    return InlineKeyboardMarkup(rows)


# ── Log görüntüleyici ────────────────────────────────────────────────────────

_LOG_LEVEL_ICON = {
    "ERROR": "🔴",
    "CRITICAL": "🔴",
    "WARNING": "🟠",
    "INFO": "⚪️",
    "DEBUG": "⚫️",
}

# Panelde gösterilecek log kanalları
_LOG_SOURCES = {
    "live": ("Canlı akış (bellek)", None, "🔴 Canlı"),
    "bot": ("bot.log", "bot.log", "📄 Bot"),
    "downloads": ("downloads.log", "downloads.log", "📥 İndirme"),
    "cookie": ("cookie_errors.log", "cookie_errors.log", "🍪 Cookie"),
}


def _read_log_tail(path, lines: int = 25) -> list[str]:
    """Dosyanın son N satırını okur (büyük dosyayı tamamen belleğe almadan)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []

    # Son ~60 KB yeterli: 25 satır için fazlasıyla.
    chunk = min(size, 60_000)
    try:
        with path.open("rb") as fh:
            fh.seek(size - chunk)
            data = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    return [ln for ln in data.splitlines() if ln.strip()][-lines:]


def _logs_text(context: ContextTypes.DEFAULT_TYPE, source: str = "live", level: str = "all") -> str:
    config = context.application.bot_data["config"]
    label, filename, _short = _LOG_SOURCES.get(source, _LOG_SOURCES["live"])

    if filename:
        entries = _read_log_tail(config.log_dir / filename, 40)
    else:
        from bot.log_buffer import last_lines
        entries = last_lines(40)

    # Seviye filtresi
    if level != "all":
        entries = [e for e in entries if f"| {level} " in e or f"| {level}|" in e]

    if not entries:
        # Mesaj duruma göre değişir: süzgeç sonuç vermediyse "kayıt yok"
        # demek yanıltıcı olur (dosya dolu ama o seviyede satır yok).
        if level != "all":
            return (
                f"<b>📜 Log — {label}</b>\n\n"
                f"<i>Bu kanalda <b>{level}</b> seviyesinde kayıt yok.</i> ✅\n\n"
                "Tüm satırları görmek için «Hepsi» süzgecini seç."
            )
        return (
            f"<b>📜 Log — {label}</b>\n\n"
            "<i>Kayıt yok — bu kanala henüz hiçbir şey yazılmamış.</i>"
        )

    entries = entries[-15:]
    body_parts = []
    for line in entries:
        icon = ""
        for name, symbol in _LOG_LEVEL_ICON.items():
            if f"| {name} " in line or f"| {name}|" in line:
                icon = symbol + " "
                break
        # Uzun satırları kırp — Telegram mesaj sınırı 4096
        trimmed = line if len(line) <= 220 else line[:220] + "…"
        body_parts.append(f"{icon}<code>{_esc(trimmed)}</code>")

    filter_note = "" if level == "all" else f" · süzgeç: <b>{level}</b>"
    return (
        f"<b>📜 Log — {label}</b> <i>(son {len(entries)}{filter_note})</i>\n\n"
        + "\n\n".join(body_parts)
    )


def _logs_keyboard(source: str = "live", level: str = "all") -> InlineKeyboardMarkup:
    source_row = [
        InlineKeyboardButton(
            ("• " if source == key else "") + short,
            callback_data=f"admin|logs|{key}|{level}",
        )
        for key, (_label, _file, short) in _LOG_SOURCES.items()
    ]

    level_row = [
        InlineKeyboardButton(
            ("• " if level == key else "") + name,
            callback_data=f"admin|logs|{source}|{key}",
        )
        for key, name in (("all", "Hepsi"), ("ERROR", "🔴 Hata"), ("WARNING", "🟠 Uyarı"))
    ]

    return InlineKeyboardMarkup([
        source_row,
        level_row,
        [
            InlineKeyboardButton("📤 Dosya İndir", callback_data=f"admin|logfile|{source}"),
            InlineKeyboardButton("🔄 Yenile", callback_data=f"admin|logs|{source}|{level}"),
        ],
        [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")],
    ])


# ── Kullanıcı arama / profil / ban yönetimi ──────────────────────────────────

def _user_label(row: dict) -> str:
    name = row.get("username")
    if name:
        return f"@{_esc(name)}"
    return _esc(row.get("first_name") or f"ID {row.get('user_id')}")


def _search_results_text(context: ContextTypes.DEFAULT_TYPE, term: str) -> tuple[str, InlineKeyboardMarkup]:
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]

    users = db.search_users(term, limit=8) if db else []
    chats = db.search_chats(term, limit=5) if db else []

    lines = [f"<b>🔍 Arama:</b> <code>{_esc(term)}</code>", ""]
    rows: list[list[InlineKeyboardButton]] = []

    if not users and not chats:
        lines.append(
            "Sonuç yok.\n\n<i>Kullanıcı adı, ad veya sayısal ID ile arayabilirsin.</i>"
        )
    else:
        if users:
            lines.append(f"<b>👤 Kullanıcılar ({len(users)})</b>")
            for row in users:
                uid = int(row["user_id"])
                banned = permissions.is_user_banned(uid)
                mark = "🚫" if banned else "✅"
                lines.append(
                    f"{mark} {_user_label(row)} — <b>{row.get('total_downloads', 0)}</b> indirme\n"
                    f"    <code>{uid}</code>"
                )
                rows.append([InlineKeyboardButton(
                    f"{mark} {_user_label(row)[:20]}", callback_data=f"admin|userinfo|{uid}"
                )])

        if chats:
            lines.append(f"\n<b>💬 Sohbetler ({len(chats)})</b>")
            for row in chats:
                cid = int(row["chat_id"])
                banned = permissions.is_group_banned(cid)
                mark = "🚫" if banned else "✅"
                title = _esc(row.get("title") or "(başlıksız)")[:30]
                lines.append(f"{mark} <b>{title}</b> — <code>{cid}</code>")
                # Sohbetler önceden yalnızca listeleniyordu, butonu yoktu:
                # panelden bir grubu banlamanın hiçbir yolu yoktu.
                rows.append([InlineKeyboardButton(
                    f"{mark} {title[:20]}", callback_data=f"admin|chatinfo|{cid}"
                )])

    rows.append([
        InlineKeyboardButton("🔍 Yeni Arama", callback_data="admin|usersearch"),
        InlineKeyboardButton("‹ Banlar", callback_data="admin|bans"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _user_info_text(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]
    live_guard = context.application.bot_data.get("live_guard")

    row = db.get_user(user_id) if db else None
    banned = permissions.is_user_banned(user_id)

    if not row:
        lines = [
            f"<b>👤 Kullanıcı</b> <code>{user_id}</code>",
            "",
            "<i>Veritabanında kaydı yok (botu hiç kullanmamış olabilir).</i>",
            f"\nBan durumu: {'🚫 <b>Banlı</b>' if banned else '✅ Banlı değil'}",
        ]
    else:
        first = datetime.fromtimestamp(row["first_seen"]).strftime("%d.%m.%Y")
        last = datetime.fromtimestamp(row["last_activity"]).strftime("%d.%m.%Y %H:%M")
        lines = [
            f"<b>👤 {_user_label(row)}</b>",
            f"<code>{user_id}</code>",
            "",
            f"📥 Toplam indirme: <b>{row['total_downloads']}</b>",
            f"📅 İlk görülme: <b>{first}</b>",
            f"🕐 Son aktivite: <b>{last}</b>",
            f"🔔 Duyuru: <b>{'kapalı' if row['broadcast_opt_out'] else 'açık'}</b>",
            f"🚷 Erişilemez: <b>{'evet' if row['is_blocked'] else 'hayır'}</b>",
            f"\nBan durumu: {'🚫 <b>Banlı</b>' if banned else '✅ Banlı değil'}",
        ]

        if live_guard:
            remaining = live_guard.ban_remaining(user_id)
            if remaining > 0:
                lines.append(
                    f"⏳ Canlı yayın banı: <b>{format_duration(remaining)}</b> kaldı"
                )

        recent = db.user_downloads(user_id, 5) if db else []
        if recent:
            lines.append("\n<b>Son indirmeler</b>")
            for item in recent:
                when = datetime.fromtimestamp(item["created_at"]).strftime("%d.%m %H:%M")
                icon = "✅" if item["result"] == "success" else "❌"
                lines.append(f"  {icon} {_esc(item['platform'] or '—')} · <i>{when}</i>")

    action = (
        InlineKeyboardButton("✅ Banı Kaldır", callback_data=f"admin|unban|{user_id}")
        if banned else
        InlineKeyboardButton("🚫 Banla", callback_data=f"admin|banask|{user_id}")
    )

    rows = [[action]]
    if live_guard and live_guard.ban_remaining(user_id) > 0:
        rows.append([InlineKeyboardButton(
            "⏳ Canlı Yayın Banını Kaldır", callback_data=f"admin|livewipe|{user_id}"
        )])
    rows.append([
        InlineKeyboardButton("🔍 Arama", callback_data="admin|usersearch"),
        InlineKeyboardButton("‹ Banlar", callback_data="admin|bans"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _chat_info_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Grup/kanal profili — kullanıcı profilinin sohbet karşılığı."""
    db = context.application.bot_data.get("db")
    permissions = context.application.bot_data["permissions"]
    manager = context.application.bot_data["process_manager"]

    rows_db = db.search_chats(str(chat_id), limit=1) if db else []
    row = rows_db[0] if rows_db else None
    banned = permissions.is_group_banned(chat_id)
    active = len([
        j for j in manager.jobs.values()
        if j.chat_id == chat_id and not j.done and not j.cancelled
    ])

    if not row:
        lines = [
            f"<b>💬 Sohbet</b> <code>{chat_id}</code>",
            "",
            "<i>Veritabanında kaydı yok.</i>",
        ]
    else:
        first = datetime.fromtimestamp(row["first_seen"]).strftime("%d.%m.%Y")
        last = datetime.fromtimestamp(row["last_activity"]).strftime("%d.%m.%Y %H:%M")
        lines = [
            f"<b>💬 {_esc(row.get('title') or '(başlıksız)')}</b>",
            f"<code>{chat_id}</code> · {_esc(row.get('chat_type') or '—')}",
            "",
            f"📥 Toplam indirme: <b>{row['total_downloads']}</b>",
            f"📅 İlk görülme: <b>{first}</b>",
            f"🕐 Son aktivite: <b>{last}</b>",
            f"🔔 Duyuru: <b>{'kapalı' if row['broadcast_opt_out'] else 'açık'}</b>",
        ]

    lines.append(f"\nBan durumu: {'🚫 <b>Banlı</b>' if banned else '✅ Banlı değil'}")
    if active:
        lines.append(f"⚙️ Aktif indirme: <b>{active}</b>")

    action = (
        InlineKeyboardButton("✅ Banı Kaldır", callback_data=f"admin|unban|{chat_id}")
        if banned else
        InlineKeyboardButton("🚫 Grubu Banla", callback_data=f"admin|banask|{chat_id}")
    )
    rows = [
        [action],
        [
            InlineKeyboardButton("🔍 Arama", callback_data="admin|usersearch"),
            InlineKeyboardButton("‹ Banlar", callback_data="admin|bans"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Ban listesi ──
def _bans_screen(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    permissions = context.application.bot_data["permissions"]
    db = context.application.bot_data.get("db")
    bans = permissions.list_bans()
    users = bans.get("users", [])
    groups = bans.get("groups", [])

    lines = ["<b>🚫 Ban Yönetimi</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []

    lines.append(f"<b>Kullanıcılar ({len(users)})</b>")
    if not users:
        lines.append("  —")
    for uid in users[:8]:
        row = db.get_user(uid) if db else None
        label = _user_label(row) if row else f"ID {uid}"
        lines.append(f"  • {label} — <code>{uid}</code>")
        # Banlı kaydın üstüne tıklanabilirlik: banı kaldırmanın tek yolu
        # /unbanid komutunu elle yazmaktı.
        rows.append([InlineKeyboardButton(
            f"👤 {label[:24]}", callback_data=f"admin|userinfo|{uid}"
        )])
    if len(users) > 8:
        lines.append(f"  …+{len(users) - 8}")

    lines.append(f"\n<b>Gruplar ({len(groups)})</b>")
    if not groups:
        lines.append("  —")
    for cid in groups[:8]:
        found = db.search_chats(str(cid), limit=1) if db else []
        title = _esc(found[0].get("title") or "(başlıksız)") if found else f"ID {cid}"
        lines.append(f"  • {title} — <code>{cid}</code>")
        rows.append([InlineKeyboardButton(
            f"💬 {title[:24]}", callback_data=f"admin|chatinfo|{cid}"
        )])
    if len(groups) > 8:
        lines.append(f"  …+{len(groups) - 8}")

    lines.append(
        "\n<i>Komutlar:</i> <code>/banid ID</code> · <code>/unbanid ID</code>\n"
        "<i>Negatif ID grubu, pozitif ID kullanıcıyı banlar.</i>"
    )

    rows.append([InlineKeyboardButton("🔍 Kullanıcı / Grup Ara", callback_data="admin|usersearch")])
    rows.append([InlineKeyboardButton("‹ Panel", callback_data="admin|panel")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Sistem durumu (status_command ile aynı bilgi, panel içinde) ──
def _system_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.application.bot_data["config"]
    manager = context.application.bot_data["process_manager"]
    active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
    ffmpeg = "✅" if shutil.which("ffmpeg") else "❌"
    gallery = "✅" if shutil.which("gallery-dl") else "❌"
    return (
        "<b>🖥 Sistem Durumu</b>\n\n"
        f"Local Bot API: <b>{'açık' if config.local_bot_api_base else 'kapalı'}</b>\n"
        f"Aktif indirme: <b>{active}</b> / {config.max_simultaneous_downloads}\n"
        f"Max dosya: <b>{config.max_file_size_mb} MB</b>\n\n"
        f"Python: <code>{sys.version.split()[0]}</code>\n"
        f"yt-dlp: <code>{yt_dlp.version.__version__}</code>\n"
        f"ffmpeg: {ffmpeg} | gallery-dl: {gallery}"
    )


# ── Kullanım listesi (sayfalı) ──
def _usage_text(context: ContextTypes.DEFAULT_TYPE, page: int, page_size: int = 8) -> tuple[str, InlineKeyboardMarkup]:
    import html as _html

    chats = context.application.bot_data["chat_registry"].all_chats()
    total = len(chats)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = chats[start:start + page_size]

    lines = [f"<b>💬 Bot Kullanımı</b> — {total} sohbet"]
    if not chunk:
        lines.append("\nHenüz kayıt yok.")
    for c in chunk:
        last = c.get("last_activity")
        when = datetime.fromtimestamp(last).strftime("%d.%m.%Y %H:%M") if last else "-"
        ctype = {"private": "özel", "group": "grup", "supergroup": "grup", "channel": "kanal"}.get(c.get("type", ""), c.get("type", "-"))
        title = _html.escape(str(c.get("title") or "(başlıksız)"))[:38]
        lines.append(
            f"\n• <b>{title}</b> <i>({ctype})</i>\n"
            f"  <code>{c.get('chat_id')}</code> · indirme: <b>{c.get('total_downloads', 0)}</b> · {when}"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"admin|usage|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="admin|noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"admin|usage|{page+1}"))

    rows = [nav, [InlineKeyboardButton("‹ Panel", callback_data="admin|panel")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Duyuru gönderimini başlatır ve panelde canlı ilerleme gösterir."""
    query = update.callback_query
    app = context.application
    draft = _bc_state(context)
    db = app.bot_data.get("db")

    text = draft.get("text")
    kind = draft.get("kind", "all")

    if not text or not db:
        await query.answer("Gönderilecek mesaj yok.", show_alert=True)
        return

    # Aynı anda tek duyuru — ikinci kez basılırsa yenisi başlamasın.
    running = app.bot_data.get("broadcast_job")
    if running and running.running:
        await query.answer("Zaten bir duyuru gönderiliyor.", show_alert=True)
        return

    targets = await asyncio.to_thread(db.broadcast_targets, kind=kind)
    job = BroadcastJob(text=text, targets=targets, kind=kind)
    app.bot_data["broadcast_job"] = job

    await query.answer("Gönderim başladı.")

    stop_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Durdur", callback_data="admin|bcstop"),
    ]])
    await _edit(query, job.progress_text(), stop_markup)

    async def on_progress(current: BroadcastJob) -> None:
        markup = stop_markup if current.running else _back_keyboard()
        await _edit(query, current.progress_text(), markup)

    async def runner() -> None:
        try:
            await run_broadcast(app, job, db=db, on_progress=on_progress)
        except Exception:
            logger.exception("Duyuru gönderimi çöktü")
        finally:
            # Taslağı temizle ki yanlışlıkla ikinci kez gönderilmesin.
            draft.pop("text", None)
            draft["awaiting"] = False
            try:
                await _edit(
                    query,
                    job.summary_text(),
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("📣 Yeni Duyuru", callback_data="admin|broadcast"),
                        InlineKeyboardButton("‹ Panel", callback_data="admin|panel"),
                    ]]),
                )
            except Exception:
                logger.exception("Duyuru özeti gösterilemedi")

    asyncio.create_task(runner())


async def broadcast_compose_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin duyuru metnini yazdığında yakalar.

    "✍️ Mesaj Yaz" sonrası admin'in ÖZEL sohbete yazdığı ilk mesaj taslak
    olarak alınır. Bu handler diğer handler'lardan önce çalışır ve link
    işleyicisinin bu mesajı indirme isteği sanmasını engeller.
    """
    if not update.effective_user or not update.message:
        return

    if not _admin_ok(update, context):
        return

    if update.effective_chat and update.effective_chat.type != "private":
        return

    draft = _bc_state(context)

    # Kullanıcı arama modu (duyuru yazma ile aynı "bekleyen giriş" mekanizması)
    if draft.get("awaiting_search"):
        draft["awaiting_search"] = False
        term = (update.message.text or "").strip()
        try:
            await update.message.delete()
        except Exception:
            pass

        text, markup = _search_results_text(context, term)
        chat_id = draft.get("search_chat_id")
        message_id = draft.get("search_message_id")
        if chat_id and message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text,
                    parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                await update.effective_chat.send_message(
                    text, parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
        raise ApplicationHandlerStop

    if not draft.get("awaiting"):
        return

    text = update.message.text_html or update.message.text or ""
    if not text.strip():
        return

    draft["text"] = text
    draft["awaiting"] = False

    # Taslağı aldık; panel mesajını güncelle ve admin'in yazdığı mesajı sil
    # (sohbet temiz kalsın, duyuru metni ortalıkta durmasın).
    try:
        await update.message.delete()
    except Exception:
        pass

    chat_id = draft.get("panel_chat_id")
    message_id = draft.get("panel_message_id")
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_broadcast_text(context),
                parse_mode="HTML",
                reply_markup=_broadcast_keyboard(context),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning("Duyuru paneli güncellenemedi, yeni mesaj gönderiliyor")
            await update.effective_chat.send_message(
                _broadcast_text(context),
                parse_mode="HTML",
                reply_markup=_broadcast_keyboard(context),
                disable_web_page_preview=True,
            )

    raise ApplicationHandlerStop


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return
    state = context.application.bot_data["bot_state"]
    await safe_reply(update.message, 
        _panel_text(context),
        parse_mode="HTML",
        reply_markup=_panel_keyboard(state),
        disable_web_page_preview=True,
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """admin| ile başlayan tüm panel callback'lerini yönetir."""
    query = update.callback_query
    if not query:
        return

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(query.from_user.id if query.from_user else None):
        await query.answer("Bu alan sadece admin için.", show_alert=True)
        return

    parts = (query.data or "").split("|")
    sub = parts[1] if len(parts) > 1 else ""
    state = context.application.bot_data["bot_state"]
    manager = context.application.bot_data["process_manager"]

    if sub == "noop":
        await query.answer()
        return

    if sub == "panel":
        await query.answer()
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "mode" and len(parts) >= 3:
        new_mode = state.set_mode(parts[2])  # state.py içinde loglanır
        await query.answer(f"Mod: {_MODE_LABEL.get(new_mode, new_mode)}")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "toggle":
        enabled = not state.get_enabled()
        state.set_enabled(enabled)
        if not enabled:
            manager.shutdown()
        await query.answer("Bot başlatıldı." if enabled else "Bot durduruldu.")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    if sub == "langmenu":
        await query.answer()
        await _edit(query, "<b>🌐 Bot dili</b>\n\nKullanıcı mesajlarının dili:", _language_keyboard(state.get_language()))
        return

    if sub == "lang" and len(parts) >= 3:
        code = parts[2]
        if code in LANGUAGES:
            state.set_language(code)   # kalıcı + loglanır
            set_language(code)         # anlık uygula
            await query.answer(f"Dil: {LANGUAGES[code]}")
        else:
            await query.answer()
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    # ESKİ CALLBACK: "📊 İstatistik" butonu "📈 Analitik" ile değiştirildi.
    # Sohbet geçmişindeki eski panel mesajlarından hâlâ tıklanabilir; ölü
    # ekran göstermek yerine yeni ekrana yönlendiriyoruz.
    if sub == "stats":
        await query.answer()
        await _edit(query, _analytics_text(context), _analytics_keyboard())
        return

    if sub == "status":
        await query.answer()
        await _edit(query, _system_text(context), _back_keyboard())
        return

    if sub == "cookie":
        await query.answer()
        await _edit(query, _cookie_text(context), _cookie_keyboard())
        return

    if sub == "cookielog":
        await query.answer()
        await _edit(query, _cookie_log_text(context), _cookie_log_keyboard())
        return

    if sub == "cookiereset":
        cookie_log = context.application.bot_data.get("cookie_log")
        if cookie_log:
            cookie_log.reset()
        await query.answer("Cookie hata sayaçları sıfırlandı.")
        await _edit(query, _cookie_text(context), _cookie_keyboard())
        return

    if sub == "bans":
        await query.answer()
        text, markup = _bans_screen(context)
        await _edit(query, text, markup)
        return

    if sub == "logs":
        source = parts[2] if len(parts) > 2 else "live"
        level = parts[3] if len(parts) > 3 else "all"
        if source not in _LOG_SOURCES:
            source = "live"
        await query.answer()
        await _edit(query, _logs_text(context, source, level), _logs_keyboard(source, level))
        return

    if sub == "logfile" and len(parts) >= 3:
        source = parts[2]
        label, filename, _short = _LOG_SOURCES.get(source, (None, None, None))
        if not filename:
            await query.answer("Bu kanal bellekte tutuluyor, dosyası yok.", show_alert=True)
            return

        path = context.application.bot_data["config"].log_dir / filename
        if not path.exists() or path.stat().st_size == 0:
            await query.answer("Dosya boş veya yok.", show_alert=True)
            return

        await query.answer("Gönderiliyor...")
        try:
            with path.open("rb") as fh:
                await query.message.reply_document(document=fh, filename=filename)
        except Exception as exc:
            logger.warning("Log dosyası gönderilemedi: %s", exc)
            await query.answer("Dosya gönderilemedi.", show_alert=True)
        return

    if sub == "usersearch":
        draft = _bc_state(context)
        draft["awaiting_search"] = True
        draft["search_chat_id"] = query.message.chat_id if query.message else None
        draft["search_message_id"] = query.message.message_id if query.message else None
        await query.answer()
        await _edit(
            query,
            "🔍 <b>Kullanıcı / sohbet ara</b>\n\n"
            "Aranacak metni gönder:\n"
            "• kullanıcı adı (<code>@arif</code> veya <code>arif</code>)\n"
            "• sayısal ID (<code>8419768278</code>)\n"
            "• grup başlığı",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("‹ Vazgeç", callback_data="admin|bans"),
            ]]),
        )
        return

    if sub == "userinfo" and len(parts) >= 3:
        try:
            uid = int(parts[2])
        except ValueError:
            await query.answer("Geçersiz ID.", show_alert=True)
            return
        text, markup = _user_info_text(context, uid)
        await query.answer()
        await _edit(query, text, markup)
        return

    if sub == "chatinfo" and len(parts) >= 3:
        try:
            cid = int(parts[2])
        except ValueError:
            await query.answer("Geçersiz ID.", show_alert=True)
            return
        text, markup = _chat_info_text(context, cid)
        await query.answer()
        await _edit(query, text, markup)
        return

    if sub == "banask" and len(parts) >= 3:
        # Geri dönüşü olan ama etkili bir aksiyon: yine de onay iste.
        try:
            tid = int(parts[2])
        except ValueError:
            await query.answer("Geçersiz ID.", show_alert=True)
            return

        permissions = context.application.bot_data["permissions"]
        is_group = permissions.is_group_id(tid)
        back = f"admin|{'chatinfo' if is_group else 'userinfo'}|{tid}"
        detail = (
            "Banlanan grupta bot hiç çalışmaz; o anda süren indirmeler de "
            "iptal edilir."
            if is_group else
            "Banlanan kullanıcı botu hiç kullanamaz ve aktif indirmesi "
            "iptal edilir."
        )

        await query.answer()
        await _edit(
            query,
            f"🚫 <b>{'Grup' if is_group else 'Kullanıcı'} banlansın mı?</b>\n\n"
            f"<code>{tid}</code>\n\n"
            f"<i>{detail} İstediğin zaman geri alabilirsin.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Evet, banla", callback_data=f"admin|ban|{tid}"),
                InlineKeyboardButton("‹ Vazgeç", callback_data=back),
            ]]),
        )
        return

    if sub in {"ban", "unban"} and len(parts) >= 3:
        try:
            tid = int(parts[2])
        except ValueError:
            await query.answer("Geçersiz ID.", show_alert=True)
            return

        permissions = context.application.bot_data["permissions"]

        # ID'nin işareti hedefi belirler: negatif → grup, pozitif → kullanıcı.
        # Panel önceden her ID'yi kullanıcı sayıyordu.
        if sub == "ban":
            is_group = permissions.ban_id(tid)
            if is_group:
                cancelled = manager.cancel_chat_jobs(tid)
                await query.answer(
                    f"Grup banlandı." + (f" {cancelled} indirme iptal edildi." if cancelled else "")
                )
            else:
                manager.cancel_user_job(tid)
                await query.answer(f"{tid} banlandı.")
        else:
            is_group = permissions.unban_id(tid)
            await query.answer(f"{tid} banı kaldırıldı.")

        if is_group:
            text, markup = _chat_info_text(context, tid)
        else:
            text, markup = _user_info_text(context, tid)
        await _edit(query, text, markup)
        return

    if sub == "livewipe" and len(parts) >= 3:
        try:
            uid = int(parts[2])
        except ValueError:
            await query.answer("Geçersiz ID.", show_alert=True)
            return
        guard = context.application.bot_data.get("live_guard")
        if guard:
            guard.clear(uid)
        await query.answer("Canlı yayın banı kaldırıldı.")
        text, markup = _user_info_text(context, uid)
        await _edit(query, text, markup)
        return

    if sub == "analytics":
        await query.answer()
        await _edit(query, _analytics_text(context), _analytics_keyboard())
        return

    if sub == "topusers":
        await query.answer()
        await _edit(query, _top_users_text(context), InlineKeyboardMarkup([[
            InlineKeyboardButton("‹ Analitik", callback_data="admin|analytics"),
        ]]))
        return

    # ── Duyuru ────────────────────────────────────────────────────────────────
    if sub == "broadcast":
        await query.answer()
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bckind" and len(parts) >= 3:
        if parts[2] in {"all", "users", "groups"}:
            _bc_state(context)["kind"] = parts[2]
        await query.answer()
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcwrite":
        draft = _bc_state(context)
        draft["awaiting"] = True
        draft["panel_chat_id"] = query.message.chat_id if query.message else None
        draft["panel_message_id"] = query.message.message_id if query.message else None
        await query.answer()
        await _edit(
            query,
            "✍️ <b>Duyuru metnini gönder</b>\n\n"
            "Şimdi bana duyuru mesajını yaz — bir sonraki mesajın taslak olarak "
            "alınacak.\n\n"
            "<i>HTML kullanabilirsin: &lt;b&gt;kalın&lt;/b&gt;, &lt;i&gt;italik&lt;/i&gt;, "
            "&lt;a href=\"...\"&gt;link&lt;/a&gt;</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("‹ Vazgeç", callback_data="admin|bccancelwrite"),
            ]]),
        )
        return

    if sub == "bccancelwrite":
        _bc_state(context)["awaiting"] = False
        await query.answer("Vazgeçildi.")
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcclear":
        draft = _bc_state(context)
        draft.pop("text", None)
        draft["awaiting"] = False
        await query.answer("Mesaj silindi.")
        await _edit(query, _broadcast_text(context), _broadcast_keyboard(context))
        return

    if sub == "bcconfirm":
        draft = _bc_state(context)
        if not draft.get("text"):
            await query.answer("Önce bir mesaj yaz.", show_alert=True)
            return

        db = context.application.bot_data.get("db")
        kind = draft.get("kind", "all")
        count = len(db.broadcast_targets(kind=kind)) if db else 0

        if not count:
            await query.answer("Bu hedef kitlede kimse yok.", show_alert=True)
            return

        await query.answer()
        await _edit(
            query,
            f"🚀 <b>Duyuru gönderilsin mi?</b>\n\n"
            f"🎯 Hedef: <b>{_BC_KIND_LABEL.get(kind, kind)}</b>\n"
            f"👥 Alıcı: <b>{count}</b> sohbet\n"
            f"⏱ Tahmini süre: <b>~{max(1, count // 20)} saniye</b>\n\n"
            "<i>Gönderim başladıktan sonra durdurabilirsin.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Evet, gönder", callback_data="admin|bcsend"),
                InlineKeyboardButton("‹ Vazgeç", callback_data="admin|broadcast"),
            ]]),
        )
        return

    if sub == "bcsend":
        await _start_broadcast(update, context)
        return

    if sub == "bcstop":
        job = context.application.bot_data.get("broadcast_job")
        if job and job.running:
            job.cancelled = True
            await query.answer("Durduruluyor...")
        else:
            await query.answer("Aktif gönderim yok.")
        return

    if sub == "close":
        await query.answer("Panel kapatıldı.")
        try:
            await query.message.delete()
        except Exception:
            await _edit(query, "<b>⚙️ Panel kapatıldı.</b> /admin ile tekrar açabilirsin.",
                        InlineKeyboardMarkup([[InlineKeyboardButton("Aç", callback_data="admin|panel")]]))
        return

    if sub == "usage" and len(parts) >= 3:
        page = int(parts[2]) if parts[2].isdigit() else 0
        text, markup = _usage_text(context, page)
        await query.answer()
        await _edit(query, text, markup)
        return

    # Geri dönüşü olmayan işlem: önce onay iste.
    # Önceden tek tıkla tüm aktif indirmeler iptal ediliyordu; yanlışlıkla
    # basıldığında kullanıcıların işi geri getirilemiyordu.
    if sub == "clear":
        active = len([j for j in manager.jobs.values() if not j.done and not j.cancelled])
        pending = len(context.application.bot_data.get("pending_jobs") or {})
        await query.answer()
        await _edit(
            query,
            "🧹 <b>Aktif işler temizlensin mi?</b>\n\n"
            f"• Devam eden indirme: <b>{active}</b>\n"
            f"• Açık format menüsü: <b>{pending}</b>\n\n"
            "<i>Bu işlem geri alınamaz; kullanıcıların devam eden "
            "indirmeleri iptal edilir.</i>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Evet, temizle", callback_data="admin|clear_yes"),
                InlineKeyboardButton("‹ Vazgeç", callback_data="admin|panel"),
            ]]),
        )
        return

    if sub == "clear_yes":
        manager.shutdown()
        cleared = await clear_all_pending(context.application)
        context.application.bot_data["playlist_sessions"] = {}
        await query.answer(f"Temizlendi. ({cleared} menü kaldırıldı)")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    await query.answer()
