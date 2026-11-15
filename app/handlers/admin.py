"""Admin command handlers: /start /addchat /addkeyword /list /remove /stats."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..config import Config
from ..db import Database

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _is_admin(message: Message, config: Config) -> bool:
    return message.from_user is not None and message.from_user.id == config.admin_id


async def _guard(message: Message, config: Config) -> bool:
    if _is_admin(message, config):
        return True
    await message.answer("⛔ Команда доступна только администратору.")
    logger.warning(
        "Unauthorized command from user_id=%s",
        message.from_user.id if message.from_user else "unknown",
    )
    return False


def _parse_chat_id(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


HELP_TEXT = (
    "🤖 <b>VA Lead Radar</b>\n\n"
    "Мониторю чаты по ключевым словам и присылаю карточки лидов.\n\n"
    "<b>Команды</b>\n"
    "/addchat &lt;chat_id&gt; — добавить чат в мониторинг\n"
    "/addkeyword &lt;слово&gt; — добавить ключевое слово\n"
    "/remove chat &lt;chat_id&gt; — удалить чат\n"
    "/remove keyword &lt;слово&gt; — удалить ключевое слово\n"
    "/list — показать чаты и ключевые слова\n"
    "/stats — статистика\n\n"
    "ℹ️ Бот видит сообщения только в тех чатах, где он участник, и получает "
    "все апдейты. Для каналов добавьте бота администратором."
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message, config: Config) -> None:
    if not await _guard(message, config):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("addchat"))
async def cmd_addchat(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    raw = (command.args or "").strip()
    chat_id = _parse_chat_id(raw) if raw else None
    title = ""
    if chat_id is None:
        await message.answer(
            "Использование: <code>/addchat -1001234567890</code>\n"
            "Подсказка: чтобы узнать id, перешлите сообщение из чата боту "
            "@userinfobot или посмотрите лог."
        )
        return

    added = await db.add_chat(chat_id, title)
    if added:
        await message.answer(f"✅ Чат <code>{chat_id}</code> добавлен в мониторинг.")
        logger.info("Chat %s added to monitoring", chat_id)
    else:
        await message.answer(f"ℹ️ Чат <code>{chat_id}</code> уже отслеживается.")


@router.message(Command("addkeyword"))
async def cmd_addkeyword(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    keyword = (command.args or "").strip()
    if not keyword:
        await message.answer(
            "Использование: <code>/addkeyword ищу разработчика</code>"
        )
        return

    if await db.add_keyword(keyword):
        await message.answer(f"✅ Ключевое слово добавлено: <b>{keyword}</b>")
        logger.info("Keyword added: %r", keyword)
    else:
        await message.answer(f"ℹ️ Ключевое слово уже есть: <b>{keyword}</b>")


@router.message(Command("list"))
async def cmd_list(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return

    chats = await db.list_chats()
    keywords = await db.list_keywords()

    lines = ["📋 <b>Мониторинг</b>", ""]
    lines.append("<b>Чаты:</b>")
    if chats:
        for chat in chats:
            label = chat.title or "без названия"
            lines.append(f"• <code>{chat.chat_id}</code> — {label}")
    else:
        lines.append("• <i>пусто</i>")

    lines.append("")
    lines.append("<b>Ключевые слова:</b>")
    if keywords:
        lines.extend(f"• {kw}" for kw in keywords)
    else:
        lines.append("• <i>пусто</i>")

    await message.answer("\n".join(lines))


@router.message(Command("remove"))
async def cmd_remove(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Использование:\n"
            "<code>/remove chat -1001234567890</code>\n"
            "<code>/remove keyword ищу разработчика</code>"
        )
        return

    parts = args.split(maxsplit=1)
    target = parts[0].lower()
    value = parts[1].strip() if len(parts) > 1 else ""

    if target in {"chat", "чат"}:
        chat_id = _parse_chat_id(value)
        if chat_id is None:
            await message.answer("Укажите числовой chat_id: <code>/remove chat -100...</code>")
            return
        if await db.remove_chat(chat_id):
            await message.answer(f"🗑 Чат <code>{chat_id}</code> удалён из мониторинга.")
            logger.info("Chat %s removed", chat_id)
        else:
            await message.answer(f"⚠️ Чат <code>{chat_id}</code> не найден.")
        return

    if target in {"keyword", "слово"}:
        if not value:
            await message.answer("Укажите слово: <code>/remove keyword ищу разработчика</code>")
            return
        if await db.remove_keyword(value):
            await message.answer(f"🗑 Ключевое слово удалено: <b>{value}</b>")
            logger.info("Keyword removed: %r", value)
        else:
            await message.answer(f"⚠️ Ключевое слово не найдено: <b>{value}</b>")
        return

    await message.answer(
        "Неизвестная цель. Используйте <code>chat</code> или <code>keyword</code>."
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return
    stats = await db.stats()
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Чатов: {stats['chats']}\n"
        f"Ключевых слов: {stats['keywords']}\n"
        f"Проанализировано сообщений: {stats['analysed']}\n"
        f"Найдено лидов: {stats['leads']}"
    )
