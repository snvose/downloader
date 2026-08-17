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
        "<b>🎨 Premium Emoji Manager</b>",
        f"<code>[{_progress_bar(assigned, total)}]</code> <b>{assigned}/{total}</b> slots customized",
        "",
    ]

    # How this works is always shown, so the admin never opens the panel and
    # gets stuck not knowing what to do.
    if last_id:
        lines.append(
            f"🎯 <b>Emoji in hand:</b> <tg-emoji emoji-id=\"{esc(last_id)}\">✨</tg-emoji> "
            f"<code>{esc(last_id)}</code>\n"
            "👇 Tap a slot below to assign it."
        )
    else:
        lines.append(
            "💡 <b>How to use</b>\n"
            "1️⃣ Send me a <b>premium emoji</b> (requires Telegram Premium)\n"
            "2️⃣ Tap where in the list you want it to show up\n"
            "3️⃣ ♻️ resets that slot back to default"
        )

    lines.append("")
    lines.append("<i>Each row describes WHERE that emoji shows up in the bot.</i>")

    rows: list[list[InlineKeyboardButton]] = []
    current_cat = None

    for sid, key, fb, ctx in SLOT_DEFS[start:end]:
        cat = category_for(sid)
        if cat != current_cat:
            current_cat = cat
            lines.append(f"\n<b>▸ {esc(cat)}</b>")

        custom_id = (data.get(key, {}) or {}).get("custom_id")
        mark = "✅" if custom_id else "⬜"
        state = "custom" if custom_id else "default"

        # Live preview: the currently used emoji on the left, where it shows on the right.
        lines.append(
            f"{mark} {em(key)} <b>{esc(ctx)}</b>\n"
            f"     <i>{state}</i> · <code>#{sid:02d}</code>"
        )

        # Button labels don't support parse_mode, so the fallback emoji is used.
        rows.append([
            InlineKeyboardButton(
                f"{mark} {fb} {ctx[:24]}", callback_data=f"emoji|set|{sid}|{page}"
            ),
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
        InlineKeyboardButton("🗑 Reset all", callback_data="emoji|resetall"),
        InlineKeyboardButton("📤 Download backup", callback_data="emoji|file"),
    ])
    rows.append([InlineKeyboardButton("‹ Admin panel", callback_data="admin|panel")])

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
            "Usage:\n"
            "<code>/emoji slot_id emoji_id</code>\n"
            "<code>/emoji slot_id reset</code>\n"
            "<code>/emojis</code>",
            parse_mode="HTML",
        )
        return

    try:
        sid = int(args[0])
    except Exception:
        await safe_reply(update.message, "Enter a valid slot ID.")
        return

    slot = ID_TO_SLOT.get(sid)
    if not slot:
        await safe_reply(update.message, "Slot not found. /emojis")
        return

    key, fb, ctx = slot

    if len(args) == 1:
        current = (load_slots().get(key, {}) or {}).get("custom_id")
        await safe_reply(update.message,
            f"<b>#{sid:02d}</b> <code>{esc(key)}</code>\n"
            f"{esc(ctx)}\n"
            f"Status: <code>{esc(str(current or 'not assigned'))}</code>\n"
            f"Preview: {em(key)}",
            parse_mode="HTML",
        )
        return

    value = str(args[1]).strip()

    if value.lower() == "reset":
        reset_slot(key)
        await safe_reply(update.message, f"Reset: <b>#{sid:02d}</b> {fb} {esc(ctx)}", parse_mode="HTML")
        return

    if not value.isdigit():
        await safe_reply(update.message, "The emoji ID must be numeric. Send me the premium emoji directly and I'll capture the ID.")
        return

    set_slot(key, value)
    context.user_data["last_emoji_id"] = value
    await safe_reply(update.message, f"Assigned: <b>#{sid:02d}</b> {em(key)} <code>{esc(key)}</code>", parse_mode="HTML")


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
        f"Selected emoji ID: <code>{esc(custom_id)}</code>\n\n"
        "Pick a slot below to assign this ID to it.\n\n"
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
        await query.answer("Admins only.", show_alert=True)
        return True

    parts = query.data.split("|")
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "noop":
        await query.answer()
        return True

    if sub == "resetall":
        await query.answer()
        await safe_query_edit(
            query,
            "🗑 <b>Reset every premium emoji assignment?</b>\n\n"
            "This can't be undone; every slot goes back to its default emoji.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, reset", callback_data="emoji|resetall_yes"),
                InlineKeyboardButton("‹ Cancel", callback_data="emoji|page|0"),
            ]]),
            disable_web_page_preview=True,
        )
        return True

    if sub == "resetall_yes":
        count = reset_all_slots()
        text, markup = _render_emoji_page(0, context.user_data.get("last_emoji_id"))
        await query.answer(f"{count} slots reset.")
        await safe_query_edit(query, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "page" and len(parts) >= 3:
        page = int(parts[2]) if parts[2].isdigit() else 0
        text, markup = _render_emoji_page(page, context.user_data.get("last_emoji_id"))
        await query.answer()
        await safe_query_edit(query, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "set" and len(parts) >= 4:
        sid = int(parts[2]) if parts[2].isdigit() else 0
        page = int(parts[3]) if parts[3].isdigit() else 0
        slot = ID_TO_SLOT.get(sid)
        last_id = context.user_data.get("last_emoji_id")

        if not slot:
            await query.answer("Slot not found.", show_alert=True)
            return True

        if not last_id:
            await query.answer("Send me a premium emoji first.", show_alert=True)
            return True

        key, _, _ = slot
        set_slot(key, str(last_id))

        text, markup = _render_emoji_page(page, str(last_id))
        await query.answer(f"#{sid:02d} updated")
        await safe_query_edit(query, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "reset" and len(parts) >= 4:
        sid = int(parts[2]) if parts[2].isdigit() else 0
        page = int(parts[3]) if parts[3].isdigit() else 0
        slot = ID_TO_SLOT.get(sid)

        if not slot:
            await query.answer("Slot not found.", show_alert=True)
            return True

        key, _, _ = slot
        reset_slot(key)

        text, markup = _render_emoji_page(page, context.user_data.get("last_emoji_id"))
        await query.answer(f"#{sid:02d} reset")
        await safe_query_edit(query, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return True

    if sub == "file":
        await query.answer("Sending file...")
        save_slots(load_slots())

        try:
            with EMOJI_FILE.open("rb") as file:
                await query.message.reply_document(document=file, filename="emoji_slots.json")
        except Exception as exc:
            await safe_reply(query.message, f"Could not send the file: {esc(exc)}")

        return True

    return False
