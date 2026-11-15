"""Shared runtime: bot, dispatcher, storage and analyzer.

Both entry points build the same runtime so handlers, the database and the LLM
client are identical in polling and webhook mode:

* ``bot.py``  — long polling (local, VPS, Docker).
* ``main.py`` — webhook + ``/health`` (free hosts with an ephemeral disk).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .analyzer import LeadAnalyzer
from .config import Config
from .db import Database, create_database
from .handlers import admin, monitor
from .logger import setup_logging

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="Справка"),
    BotCommand(command="addchat", description="Добавить чат в мониторинг"),
    BotCommand(command="addkeyword", description="Добавить ключевое слово"),
    BotCommand(command="remove", description="Удалить чат или слово"),
    BotCommand(command="pause", description="Пауза для чата"),
    BotCommand(command="resume", description="Снять паузу"),
    BotCommand(command="list", description="Показать чаты и слова"),
    BotCommand(command="settings", description="Текущие настройки"),
    BotCommand(command="export", description="Выгрузить настройки файлом"),
    BotCommand(command="stats", description="Статистика"),
]


@dataclass
class Runtime:
    config: Config
    bot: Bot
    dispatcher: Dispatcher
    db: Database
    analyzer: LeadAnalyzer

    async def shutdown(self) -> None:
        await self.analyzer.aclose()
        await self.db.close()
        await self.bot.session.close()


async def _connect_with_retry(
    db: Database, attempts: int = 8, delay_seconds: float = 3.0
) -> None:
    """Connect to storage, retrying while a managed database wakes up.

    Neon suspends its compute after ~5 idle minutes; the first connection after
    that can take a few seconds. Retrying means a sleeping database does not kill
    startup.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await db.connect()
            return
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            if attempt == attempts:
                break
            logger.warning(
                "Database connect attempt %s/%s failed: %s. Retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay_seconds,
            )
            await asyncio.sleep(delay_seconds)
    raise RuntimeError(f"Could not reach the database after {attempts} attempts") from last_error


async def build_runtime(config: Config) -> Runtime:
    """Create storage, the LLM client, the bot and the dispatcher."""
    db = create_database(config)
    await _connect_with_retry(db)
    seeded = await db.seed_keywords(config.default_keywords)
    if seeded:
        logger.info("Applied %d default keyword(s)", seeded)

    analyzer = LeadAnalyzer(config)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(config=config, db=db, analyzer=analyzer)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(monitor.router)

    if not config.admin_id:
        logger.warning(
            "ADMIN_ID is not set: leads are recorded but no cards are delivered. "
            "Set ADMIN_ID and redeploy."
        )

    return Runtime(
        config=config, bot=bot, dispatcher=dispatcher, db=db, analyzer=analyzer
    )


async def set_commands(bot: Bot) -> None:
    """Publish the command menu; failure is non-fatal."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not set bot commands: %s", exc)


__all__ = [
    "Runtime",
    "build_runtime",
    "set_commands",
    "setup_logging",
]
