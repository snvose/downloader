from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from bot.safe_message import safe_query_edit, safe_reply
from telegram.constants import MessageEntityType
from telegram.ext import ContextTypes

from bot.emoji_manager import (
    EMOJI_FILE,
    ID_TO_SLOT,
    SLOT_DEFS,
    assigned_count,
    category_for,
    eb,
    em,
    ensure_file,
    load_slots,
    reset_all_slots,
    reset_slot,
    save_slots,
    set_slot,
)
from bot.ui import esc


EMOJI_PAGE_SIZE = 8


def _progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = 0 if total == 0 else int(done / total * width)
    return "█" * filled + "░" * (width - filled)


def _render_emoji_page(page: int, last_id: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    ensure_file()

    total = len(SLOT_DEFS)
    pages = max(1, (total + EMOJI_PAGE_SIZE - 1) // EMOJI_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * EMOJI_PAGE_SIZE
    end = min(start + EMOJI_PAGE_SIZE, total)
    data = load_slots()
    assigned = assigned_count()

    lines = [
        f"<b>{em('btn_emoji')} Premium Emoji Yönetimi</b>",
        f"<code>[{_progress_bar(assigned, total)}]</code> <b>{assigned}/{total}</b> atandı",
    ]
    if last_id:
        lines.append(f"🎯 Seçili ID: <code>{esc(last_id)}</code> — bir slota dokun.")
    else:
        lines.append("💡 Atamak için önce bana bir premium emoji gönder.")
    lines.append("")

    rows: list[list[InlineKeyboardButton]] = []
    current_cat = None

    for sid, key, fb, ctx in SLOT_DEFS[start:end]:
        cat = category_for(sid)
        if cat != current_cat:
            current_cat = cat
            lines.append(f"\n<b>▸ {esc(cat)}</b>")
        custom_id = (data.get(key, {}) or {}).get("custom_id")
        mark = "✅" if custom_id else "⬜"
        lines.append(f"{mark} <b>#{sid:02d}</b> {em(key)} <code>{key}</code> — {esc(ctx)}")
        rows.append([
            InlineKeyboardButton(f"{mark} #{sid:02d} {fb} {ctx[:22]}", callback_data=f"emoji|set|{sid}|{page}"),
            InlineKeyboardButton("♻️", callback_data=f"emoji|reset|{sid}|{page}"),
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"emoji|page|{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{pages}", callback_data="emoji|noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"emoji|page|{page+1}"))
    rows.append(nav)

    rows.append([
        InlineKeyboardButton("🗑 Tümünü Sıfırla", callback_data="emoji|resetall"),
        InlineKeyboardButton("📤 Dosya", callback_data="emoji|file"),
    ])
    rows.append([InlineKeyboardButton(eb("menu_back", "‹ Owner"), callback_data="menu|owner")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def emojiler_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(update.effective_user.id):
        return

    last_id = context.user_data.get("last_emoji_id")
    text, markup = _render_emoji_page(0, last_id)

    await safe_reply(update.message,
        text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(update.effective_user.id):
        return

    args = context.args or []

    if not args:
        await safe_reply(update.message,
            "Kullanım:\n"
            "<code>/emoji slot_id emoji_id</code>\n"
            "<code>/emoji slot_id reset</code>\n"
            "<code>/emojiler</code>",
            parse_mode="HTML",
        )
        return

    try:
        sid = int(args[0])
    except Exception:
        await safe_reply(update.message,"Geçerli slot ID gir.")
        return

    slot = ID_TO_SLOT.get(sid)
    if not slot:
        await safe_reply(update.message,"Slot bulunamadı. /emojiler")
        return

    key, fb, ctx = slot

    if len(args) == 1:
        current = (load_slots().get(key, {}) or {}).get("custom_id")
        await safe_reply(update.message,
            f"<b>#{sid:02d}</b> <code>{esc(key)}</code>\n"
            f"{esc(ctx)}\n"
            f"Durum: <code>{esc(str(current or 'atanmadı'))}</code>\n"
            f"Önizleme: {em(key)}",
            parse_mode="HTML",
        )
        return

    value = str(args[1]).strip()

    if value.lower() == "reset":
        reset_slot(key)
        await safe_reply(update.message,f"Sıfırlandı: <b>#{sid:02d}</b> {fb} {esc(ctx)}", parse_mode="HTML")
        return

    if not value.isdigit():
        await safe_reply(update.message,"Emoji ID sayısal olmalı. Premium emojiyi bana özelden gönder, ID'yi yakalayayım.")
        return

    set_slot(key, value)
    context.user_data["last_emoji_id"] = value
    await safe_reply(update.message,f"Atandı: <b>#{sid:02d}</b> {em(key)} <code>{esc(key)}</code>", parse_mode="HTML")


async def emoji_detect_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.effective_message:
        return

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(update.effective_user.id):
        return

    if update.effective_chat.type != "private":
        return

    message = update.effective_message
    entities = list(message.entities or []) + list(message.caption_entities or [])

    ids: list[str] = []
    for entity in entities:
        if entity.type == MessageEntityType.CUSTOM_EMOJI:
            custom_id = getattr(entity, "custom_emoji_id", None)
            if custom_id and str(custom_id).isdigit() and str(custom_id) not in ids:
                ids.append(str(custom_id))

    if not ids:
        return

    custom_id = ids[0]
    context.user_data["last_emoji_id"] = custom_id

    text, markup = _render_emoji_page(0, custom_id)

    await safe_reply(message,
        f"Seçili emoji ID: <code>{esc(custom_id)}</code>\n\n"
        "Aşağıdan slot seçersen bu ID o slota atanır.\n\n"
        + text,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def handle_emoji_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("emoji|"):
        return False

    permissions = context.application.bot_data["permissions"]
    if not permissions.is_admin(query.from_user.id):
        await query.answer("Bu alan sadece owner için.", show_alert=True)
        return True

    parts = query.data.split("|")
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "noop":
        await query.answer()
        return True

    # Tümünü sıfırla — önce onay iste
    if sub == "resetall":
        await query.answer()
        await safe_query_edit(
            query,
            "🗑 <b>Tüm premium emoji atamaları sıfırlansın mı?</b>\n\n"
            "Bu işlem geri alınamaz; tüm slotlar varsayılan emojilere döner.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Evet, sıfırla", callback_data="emoji|resetall_yes"),
                InlineKeyboardButton("‹ Vazgeç", callback_data="emoji|page|0"),
            ]]),
            disable_web_page_preview=True,
        )
        return True

    if sub == "resetall_yes":
        count = reset_all_slots()
        text, markup = _render_emoji_page(0, context.user_data.get("last_emoji_id"))
        await query.answer(f"{count} slot sıfırlandı.")
        await safe_query_edit(query, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "page" and len(parts) >= 3:
        page = int(parts[2]) if parts[2].isdigit() else 0
        text, markup = _render_emoji_page(page, context.user_data.get("last_emoji_id"))
        await query.answer()
        await safe_query_edit(query,text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "set" and len(parts) >= 4:
        sid = int(parts[2]) if parts[2].isdigit() else 0
        page = int(parts[3]) if parts[3].isdigit() else 0
        slot = ID_TO_SLOT.get(sid)
        last_id = context.user_data.get("last_emoji_id")

        if not slot:
            await query.answer("Slot bulunamadı.", show_alert=True)
            return True

        if not last_id:
            await query.answer("Önce bana premium emoji gönder.", show_alert=True)
            return True

        key, _, _ = slot
        set_slot(key, str(last_id))

        text, markup = _render_emoji_page(page, str(last_id))
        await query.answer(f"#{sid:02d} güncellendi")
        await safe_query_edit(query,text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "reset" and len(parts) >= 4:
        sid = int(parts[2]) if parts[2].isdigit() else 0
        page = int(parts[3]) if parts[3].isdigit() else 0
        slot = ID_TO_SLOT.get(sid)

        if not slot:
            await query.answer("Slot bulunamadı.", show_alert=True)
            return True

        key, _, _ = slot
        reset_slot(key)

        text, markup = _render_emoji_page(page, context.user_data.get("last_emoji_id"))
        await query.answer(f"#{sid:02d} sıfırlandı")
        await safe_query_edit(query,text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "file":
        await query.answer("Dosya gönderiliyor...")
        save_slots(load_slots())

        try:
            with EMOJI_FILE.open("rb") as file:
                await query.message.reply_document(document=file, filename="emoji_slots.json")
        except Exception as exc:
            await safe_reply(query.message,f"Dosya gönderilemedi: {esc(exc)}")

        return True

    return False
