from __future__ import annotations

import html
import shutil
import sys
from datetime import datetime

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.i18n import LANGUAGES, set_language
from bot.cookie_health import platform_cookie_status
from bot.pending import clear_all_pending
from bot.state import MODE_MAINTENANCE, MODE_NORMAL, MODE_SAFE
from bot.storage import read_json


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

    await update.message.reply_text("Bot durduruldu. Aktif işler iptal edildi.")


async def basla_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    permissions = context.application.bot_data["permissions"]
    permissions.set_bot_enabled(True)

    await update.message.reply_text("Bot başlatıldı.")


async def banid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await update.message.reply_text("Kullanım: /banid USER_ID")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID sayısal olmalı.")
        return

    permissions = context.application.bot_data["permissions"]
    permissions.ban_user(user_id)

    manager = context.application.bot_data["process_manager"]
    manager.cancel_user_job(user_id)

    await update.message.reply_text(f"Banlandı: {user_id}")


async def unbanid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return

    if not context.args:
        await update.message.reply_text("Kullanım: /unbanid USER_ID")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID sayısal olmalı.")
        return

    permissions = context.application.bot_data["permissions"]
    permissions.unban_user(user_id)

    await update.message.reply_text(f"Ban kaldırıldı: {user_id}")


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

    await update.message.reply_text(
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
    await update.message.reply_text(f"Tüm işler temizlendi. ({cleared} menü kaldırıldı)")


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
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True,
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
            InlineKeyboardButton("📊 İstatistik", callback_data="admin|stats"),
            InlineKeyboardButton("🖥 Sistem", callback_data="admin|status"),
        ],
        [
            InlineKeyboardButton("💬 Kullanım", callback_data="admin|usage|0"),
            InlineKeyboardButton("🚫 Banlar", callback_data="admin|bans"),
            InlineKeyboardButton("🍪 Cookie", callback_data="admin|cookie"),
        ],
        [
            InlineKeyboardButton("🎨 Emoji", callback_data="emoji|page|0"),
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
    s = context.application.bot_data["chat_registry"].stats()
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


# ── İstatistik görünümü ──
def _stats_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.application.bot_data["config"]
    data = read_json(config.data_dir / "usage_stats.json", {})
    plats = data.get("platforms", {}) if isinstance(data, dict) else {}
    plat_lines = "\n".join(
        f"  • {name}: <b>{count}</b>"
        for name, count in sorted(plats.items(), key=lambda x: -x[1])
    ) or "  • —"
    return (
        "<b>📊 İstatistikler</b>\n\n"
        f"Toplam indirme: <b>{data.get('total_downloads', 0)}</b>\n"
        f"Başarısız: <b>{data.get('failed_downloads', 0)}</b>\n"
        f"İptal edilen: <b>{data.get('cancelled_downloads', 0)}</b>\n\n"
        f"<b>Platform dağılımı</b>\n{plat_lines}"
    )


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


# ── Ban listesi ──
def _bans_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    permissions = context.application.bot_data["permissions"]
    bans = permissions.list_bans()
    users = bans.get("users", [])
    groups = bans.get("groups", [])
    u_list = "\n".join(f"  • <code>{u}</code>" for u in users[:15]) or "  —"
    g_list = "\n".join(f"  • <code>{g}</code>" for g in groups[:15]) or "  —"
    extra_u = f"\n  …+{len(users)-15}" if len(users) > 15 else ""
    extra_g = f"\n  …+{len(groups)-15}" if len(groups) > 15 else ""
    return (
        "<b>🚫 Ban Yönetimi</b>\n\n"
        f"<b>Kullanıcılar ({len(users)})</b>\n{u_list}{extra_u}\n\n"
        f"<b>Gruplar ({len(groups)})</b>\n{g_list}{extra_g}\n\n"
        "<i>Komutlar:</i> <code>/banid ID</code> · <code>/unbanid ID</code>"
    )


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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _admin_ok(update, context):
        return
    state = context.application.bot_data["bot_state"]
    await update.message.reply_text(
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

    if sub == "stats":
        await query.answer()
        await _edit(query, _stats_text(context), _back_keyboard())
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
        await _edit(query, _bans_text(context), _back_keyboard())
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

    if sub == "clear":
        manager.shutdown()
        await clear_all_pending(context.application)
        context.application.bot_data["playlist_sessions"] = {}
        await query.answer("Tüm aktif işler temizlendi.")
        await _edit(query, _panel_text(context), _panel_keyboard(state))
        return

    await query.answer()
