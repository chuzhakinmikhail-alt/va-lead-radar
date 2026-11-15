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

        if not config.admin_id:
            logger.warning(
                "Lead detected (chat=%s msg=%s) but ADMIN_ID is not set; "
                "card not delivered.",
                chat_id,
                message.message_id,
            )
            return

        try:
            await bot.send_message(
                config.admin_id,
                format_lead_card(message, verdict, matched),
                disable_web_page_preview=True,
            )
            logger.info(
                "Lead forwarded to admin (chat=%s msg=%s intent=%r)",
                chat_id,
                message.message_id,
                verdict.intent,
            )
        except Exception as exc:
            logger.error("Failed to send lead card to admin: %s", exc)

    except Exception as exc:  # never let one message kill the dispatcher
        logger.exception("Error while processing message in chat %s: %s", chat_id, exc)
