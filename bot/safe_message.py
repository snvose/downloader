from __future__ import annotations

import re
from typing import Any

from telegram.error import BadRequest


TG_EMOJI_RE = re.compile(
    r'<tg-emoji emoji-id="\d+">.*?</tg-emoji>',
    re.DOTALL,
)


def strip_custom_emoji(text: str) -> str:
    return TG_EMOJI_RE.sub("✨", str(text or ""))


def is_entity_error(exc: Exception) -> bool:
    raw = str(exc)
    return (
        "Entity_text_invalid" in raw
        or "can't parse entities" in raw.lower()
        or "can't find end tag" in raw.lower()
    )


async def safe_reply(message: Any, text: str, **kwargs: Any):
    try:
        return await message.reply_text(text, **kwargs)
    except BadRequest as exc:
        if not is_entity_error(exc):
            raise

        kwargs["parse_mode"] = "HTML"
        return await message.reply_text(strip_custom_emoji(text), **kwargs)


async def safe_message_edit(message: Any, text: str, **kwargs: Any):
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if not is_entity_error(exc):
            raise

        kwargs["parse_mode"] = "HTML"
        return await message.edit_text(strip_custom_emoji(text), **kwargs)


async def safe_query_edit(query: Any, text: str, **kwargs: Any):
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if not is_entity_error(exc):
            raise

        kwargs["parse_mode"] = "HTML"
        return await query.edit_message_text(strip_custom_emoji(text), **kwargs)
