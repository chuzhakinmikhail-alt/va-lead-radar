"""Message monitor: match keywords, classify with the LLM, forward lead cards."""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from ..analyzer import LeadAnalyzer, LeadVerdict
from ..config import Config
from ..db import Database, message_hash

logger = logging.getLogger(__name__)
router = Router(name="monitor")

MONITORED_TYPES = {
    ChatType.GROUP,
    ChatType.SUPERGROUP,
    ChatType.CHANNEL,
}


def _extract_text(message: Message) -> str:
    """Return the message's textual content (text or media caption)."""
    return (message.text or message.caption or "").strip()


async def _auto_register(message: Message, db: Database) -> bool:
    """Adopt a chat in auto mode; False when it stays unmonitored.

    ``add_chat`` returns True only for a newly inserted row, which avoids
    listing every chat on the hot path. A chat that already exists is left
    alone, so a paused chat is never revived by an incoming message.
    """
    chat = message.chat
    if chat.type not in MONITORED_TYPES:
        return False

    title = chat.title or chat.full_name or ""
    if await db.add_chat(chat.id, title):
        logger.info("Auto-monitoring new chat %s (%s)", chat.id, title or "без названия")
        return True

    return await db.is_monitored(chat.id)


def build_message_link(message: Message) -> str:
    """Best-effort public link to a message."""
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        internal = chat_id[4:]
        return f"https://t.me/c/{internal}/{message.message_id}"
    return ""


def format_lead_card(
    message: Message, verdict: LeadVerdict, matched: list[str]
) -> str:
    chat = message.chat
    author = message.from_user
    if author is None:
        author_name = chat.title or "канал"
        author_ref = ""
    else:
        author_name = author.full_name or (author.username or "")
        author_ref = f"@{author.username}" if author.username else f"id {author.id}"

    chat_label = chat.title or chat.full_name or str(chat.id)
    link = build_message_link(message)

    lines = [
        "🎯 <b>Новый лид</b>",
        "",
        f"<b>Чат:</b> {html.escape(chat_label)} "
        f"(<code>{chat.id}</code>)",
        f"<b>Автор:</b> {html.escape(author_name)} {html.escape(author_ref)}".rstrip(),
        f"<b>Интент:</b> {html.escape(verdict.intent)}",
        f"<b>Саммари:</b> {html.escape(verdict.summary)}",
    ]
    if matched:
        lines.append(f"<b>Совпало:</b> {html.escape(', '.join(matched))}")
    if link:
        lines.append(f"<b>Ссылка:</b> <a href=\"{link}\">перейти к сообщению</a>")
    else:
        lines.append(f"<b>Сообщение:</b> id <code>{message.message_id}</code>")

    lines.append("")
    lines.append("<b>Текст:</b>")
    lines.append(f"<blockquote>{html.escape(_extract_text(message))[:1000]}</blockquote>")
    return "\n".join(lines)


@router.message(F.chat.type.in_(MONITORED_TYPES))
async def on_message(
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    analyzer: LeadAnalyzer,
) -> None:
    """Handle every group/channel message and decide whether it is a lead."""
    chat_id = message.chat.id
    try:
        if not await db.is_monitored(chat_id):
            if not config.monitor_all_chats:
                return
            # Auto mode: adopt the chat on its first message. A paused chat is
            # registered but is_monitored() is False, so this never revives it.
            if not await _auto_register(message, db):
                return

        text = _extract_text(message)
        if not text:
            return

        matched = await db.match_keywords(text)
        if not matched:
            return

        msg_hash = message_hash(chat_id, message.message_id)
        if await db.is_duplicate(msg_hash):
            logger.debug("Duplicate message skipped: %s", msg_hash)
            return

        verdict = await analyzer.analyze(text)

        # Record every analysed message (lead or not) so the hash cannot be
        # processed twice and stats stay meaningful.
        newly = await db.record_lead(
            msg_hash=msg_hash,
            chat_id=chat_id,
            message_id=message.message_id,
            is_lead=verdict.is_lead,
            summary=verdict.summary,
            intent=verdict.intent,
        )
        if not newly:
            logger.debug("Race: message already recorded: %s", msg_hash)
            return

        if not verdict.is_lead:
            logger.info("Not a lead (chat %s): %s", chat_id, matched)
            return

        if not config.recipients:
            logger.warning(
                "Lead detected (chat=%s msg=%s) but no admin is configured; "
                "card not delivered.",
                chat_id,
                message.message_id,
            )
            return

        card = format_lead_card(message, verdict, matched)
        delivered = 0
        for admin_id in config.recipients:
            try:
                await bot.send_message(
                    admin_id, card, disable_web_page_preview=True
                )
                delivered += 1
            except Exception as exc:  # one unreachable admin must not stop the rest
                logger.error(
                    "Failed to send lead card to admin %s: %s", admin_id, exc
                )
        if delivered:
            logger.info(
                "Lead forwarded to %s admin(s) (chat=%s msg=%s intent=%r)",
                delivered,
                chat_id,
                message.message_id,
                verdict.intent,
            )

    except Exception as exc:  # never let one message kill the dispatcher
        logger.exception("Error while processing message in chat %s: %s", chat_id, exc)
