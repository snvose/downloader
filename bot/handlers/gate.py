from __future__ import annotations

"""
Global ban gate for banned users/groups.

Ban checks used to live inside each handler and were incomplete: the button
(callback) handler had none at all, /start and /cancel only checked the user
ban and never the group ban. A banned group could still use menu buttons and
some commands.

This gate runs at the lowest handler group (-2) and raises
ApplicationHandlerStop when banned, stopping every other handler. A newly
added handler can never forget the ban check.

Maintenance mode is INTENTIONALLY not handled here: its message depends on
the command (see Permissions.check_update) and commands like /cancel still
need to work during maintenance.
"""

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..i18n import t

logger = logging.getLogger("downloader")


async def ban_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    permissions = context.application.bot_data.get("permissions")
    if permissions is None:
        return

    user = update.effective_user
    chat = update.effective_chat

    user_id = user.id if user else None
    chat_id = chat.id if chat else None

    # The admin is never stopped here (avoids locking themself out).
    if permissions.is_admin(user_id):
        return

    if permissions.is_user_banned(user_id):
        reason = t("banned_user")
    elif permissions.is_group_banned(chat_id):
        reason = t("banned_group")
    else:
        return

    # A button press gets a private alert; messages are ignored silently —
    # replying to every message in a banned chat would just be spam.
    query = update.callback_query
    if query is not None:
        try:
            await query.answer(reason, show_alert=True)
        except Exception:
            pass

    raise ApplicationHandlerStop
