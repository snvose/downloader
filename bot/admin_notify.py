from __future__ import annotations

"""
Admin failure notification: a summary, the last 20 log lines and two quick
mode-switch buttons whenever a download cannot be completed.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .log_buffer import last_lines

logger = logging.getLogger("downloader")


def _notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔇 Safe mode", callback_data="admin|mode|safe"),
            InlineKeyboardButton("🛠 Maintenance", callback_data="admin|mode|maintenance"),
        ],
        [InlineKeyboardButton("⚙️ Admin panel", callback_data="admin|panel")],
    ])


async def notify_admin_failure(
    bot,
    admin_id: int,
    *,
    summary: str,
    url: str = "",
    platform: str = "",
    user_id: int | None = None,
    username: str | None = None,
    chat_id: int | None = None,
    chat_title: str | None = None,
) -> None:
    if not admin_id:
        return

    tail = "\n".join(last_lines(20))
    if len(tail) > 3000:
        tail = tail[-3000:]

    if username:
        user_line = f"@{html.escape(username)} (id: {user_id or '-'})"
    else:
        user_line = f"id: {user_id or '-'}"

    if chat_title:
        chat_line = f"{html.escape(str(chat_title))} (id: {chat_id if chat_id is not None else '-'})"
    else:
        chat_line = f"id: {chat_id if chat_id is not None else '-'}"

    text = (
        "<b>Download failed</b>\n\n"
        f"<b>User:</b> {user_line}\n"
        f"<b>Chat:</b> {chat_line}\n"
        f"<b>Summary:</b> {html.escape(summary[:400])}\n"
        f"<b>Platform:</b> {html.escape(platform or '-')}\n"
        f"<b>URL:</b> {html.escape(url[:300] or '-')}\n\n"
        f"<b>Last 20 log lines:</b>\n<pre>{html.escape(tail)}</pre>"
    )

    try:
        await bot.send_message(
            chat_id=admin_id,
            text=text[:4096],
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_notify_keyboard(),
        )
    except Exception:
        logger.exception("Could not deliver the admin failure notification.")
