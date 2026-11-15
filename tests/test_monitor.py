"""End-to-end test of the monitor pipeline.

Uses the real Database, real keyword matching and real card formatting. Only the
Telegram network call and the LLM HTTP call are replaced, since they cannot run
offline.
"""

from datetime import datetime

import pytest
from aiogram.types import Chat, Message, User

from app.analyzer import LeadVerdict
from app.config import Config
from app.db import SQLiteDatabase
from app.handlers.monitor import on_message


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeAnalyzer:
    def __init__(self, verdict: LeadVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    async def analyze(self, text: str) -> LeadVerdict:
        self.calls += 1
        return self.verdict


def make_config(admin_id: int = 42) -> Config:
    return Config(
        bot_token="1:abc",
        admin_id=admin_id,
        llm_api_key="k",
        llm_provider="openai",
    )


def make_message(text: str, chat_id: int = -1001, message_id: int = 10) -> Message:
    chat = Chat(id=chat_id, type="supergroup", title="Dev Chat", username="devchat")
    user = User(id=7, is_bot=False, first_name="Иван", username="ivan")
    return Message(
        message_id=message_id,
        date=datetime(2026, 1, 1),
        chat=chat,
        from_user=user,
        text=text,
    )


@pytest.mark.asyncio
async def test_lead_is_forwarded_once(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        await db.add_keyword("нужен бот")
        bot = FakeBot()
        analyzer = FakeAnalyzer(LeadVerdict(True, "Хочет бота", "разработка бота"))
        cfg = make_config()

        msg = make_message("Привет, нужен бот для магазина")
        await on_message(msg, bot, cfg, db, analyzer)
        # second delivery of the same message must be ignored (anti-duplicate)
        await on_message(msg, bot, cfg, db, analyzer)

        assert len(bot.sent) == 1
        admin_id, card = bot.sent[0]
        assert admin_id == cfg.admin_id
        assert "разработка бота" in card and "https://t.me/devchat/10" in card
        assert analyzer.calls == 1  # LLM not called twice
        stats = await db.stats()
        assert stats["leads"] == 1 and stats["analysed"] == 1


@pytest.mark.asyncio
async def test_spam_not_forwarded_but_recorded(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        await db.add_keyword("нужен бот")
        bot = FakeBot()
        analyzer = FakeAnalyzer(LeadVerdict(False, "", "реклама"))

        await on_message(make_message("нужен бот!! купите курс"), bot, make_config(), db, analyzer)

        assert bot.sent == []
        stats = await db.stats()
        assert stats["leads"] == 0 and stats["analysed"] == 1


@pytest.mark.asyncio
async def test_unmonitored_chat_is_ignored(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("нужен бот")  # keyword exists but chat not monitored
        bot = FakeBot()
        analyzer = FakeAnalyzer(LeadVerdict(True, "s", "i"))
        await on_message(make_message("нужен бот"), bot, make_config(), db, analyzer)
        assert bot.sent == []
        assert analyzer.calls == 0


@pytest.mark.asyncio
async def test_message_without_keyword_is_ignored(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001)
        await db.add_keyword("нужен бот")
        bot = FakeBot()
        analyzer = FakeAnalyzer(LeadVerdict(True, "s", "i"))
        await on_message(make_message("просто болтаем"), bot, make_config(), db, analyzer)
        assert bot.sent == []
        assert analyzer.calls == 0


@pytest.mark.asyncio
async def test_lead_recorded_but_not_delivered_without_admin(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001)
        await db.add_keyword("нужен бот")
        bot = FakeBot()
        analyzer = FakeAnalyzer(LeadVerdict(True, "Хочет бота", "разработка бота"))
        await on_message(
            make_message("нужен бот для магазина"), bot, make_config(admin_id=0), db, analyzer
        )
        # No ADMIN_ID => nothing delivered, but the lead is still recorded once.
        assert bot.sent == []
        stats = await db.stats()
        assert stats["leads"] == 1 and stats["analysed"] == 1
