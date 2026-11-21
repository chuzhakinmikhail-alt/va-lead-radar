"""Tests for the source-management admin commands and the poller wiring.

The handlers are called directly with a Message stand-in, so the real command
logic (validation, normalisation, storage) runs without Telegram.
"""

import pytest

from app.config import Config
from app.db import SQLiteDatabase
from app.handlers.admin import (
    _settings_text,
    cmd_addkeyword,
    cmd_addsource,
    cmd_list,
    cmd_pause_source,
    cmd_poll,
    cmd_removesource,
    cmd_sources,
)


class FakeMessage:
    def __init__(self, user_id: int = 42, chat_type: str = "private") -> None:
        self.chat = type("Chat", (), {"id": 555, "type": chat_type})()
        self.from_user = type("User", (), {"id": user_id})()
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1]


class FakeCommand:
    def __init__(self, command: str, args: str = "") -> None:
        self.command = command
        self.args = args


class FakePoller:
    def __init__(self, stats=None, error=None) -> None:
        self.stats = stats or {
            "sources": 2,
            "items": 5,
            "matched": 3,
            "leads": 1,
            "sent": 1,
        }
        self.error = error
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.stats


def make_config(**overrides) -> Config:
    base = dict(bot_token="1:abc", admin_id=42, llm_api_key="k")
    base.update(overrides)
    return Config(**base)


@pytest.mark.asyncio
async def test_addsource_stores_rss_feed(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addsource(
            message,
            FakeCommand("addsource", "rss https://www.freelancer.com/rss.xml"),
            make_config(),
            db,
        )
        assert "✅" in message.last
        sources = await db.list_sources()
        assert sources[0].kind == "rss"
        assert sources[0].target == "https://www.freelancer.com/rss.xml"


@pytest.mark.asyncio
async def test_addsource_normalises_channel_spellings(tmp_path):
    """@name, t.me/name and name must all resolve to one source."""
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        config = make_config()
        for raw in ("@orders", "https://t.me/orders", "t.me/s/orders"):
            await cmd_addsource(
                FakeMessage(), FakeCommand("addsource", f"tg_public {raw}"), config, db
            )
        sources = await db.list_sources()
        assert len(sources) == 1
        assert sources[0].source_id == "tg_public:orders"


@pytest.mark.asyncio
async def test_addsource_rejects_unknown_kind(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addsource(
            message, FakeCommand("addsource", "magic https://x"), make_config(), db
        )
        assert "Неизвестный тип" in message.last
        assert await db.list_sources() == []


@pytest.mark.asyncio
async def test_addsource_rejects_private_channel(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addsource(
            message,
            FakeCommand("addsource", "tg_public +AbCdEf123456"),
            make_config(),
            db,
        )
        assert "не похоже на публичный канал" in message.last
        assert await db.list_sources() == []


@pytest.mark.asyncio
async def test_addsource_warns_when_userbot_is_not_configured(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addsource(
            message, FakeCommand("addsource", "userbot @some_chat"), make_config(), db
        )
        assert "TELEGRAM_SESSION" in message.answers[0]
        # Still stored: it starts working once the credentials appear.
        assert (await db.list_sources())[0].kind == "userbot"


@pytest.mark.asyncio
async def test_removesource_deletes_by_normalised_id(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        message = FakeMessage()
        await cmd_removesource(
            message, FakeCommand("removesource", "tg_public @orders"), make_config(), db
        )
        assert "🗑" in message.last
        assert await db.list_sources() == []


@pytest.mark.asyncio
async def test_pause_source_toggles_state(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("rss", "https://x/feed", "X")
        config = make_config()
        await cmd_pause_source(
            FakeMessage(),
            FakeCommand("pause_source", "rss https://x/feed"),
            config,
            db,
        )
        assert (await db.list_sources())[0].paused is True
        assert await db.list_sources(include_paused=False) == []

        await cmd_pause_source(
            FakeMessage(),
            FakeCommand("resume_source", "rss https://x/feed"),
            config,
            db,
        )
        assert (await db.list_sources())[0].paused is False


@pytest.mark.asyncio
async def test_sources_lists_state_and_help(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        message = FakeMessage()
        await cmd_sources(message, make_config(), db)
        assert "tg_public:orders" in message.last
        assert "addsource" in message.last


@pytest.mark.asyncio
async def test_list_shows_sources(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("rss", "https://x/feed", "X")
        message = FakeMessage()
        await cmd_list(message, make_config(), db)
        assert "rss:https://x/feed" in message.last
        assert "Внешние источники" in message.last


@pytest.mark.asyncio
async def test_poll_reports_counters(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        poller = FakePoller()
        await cmd_poll(message, make_config(), poller)
        assert poller.calls == 1
        assert "Лидов: 1" in message.last


@pytest.mark.asyncio
async def test_poll_reports_failure_without_crashing(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        poller = FakePoller(error=RuntimeError("boom"))
        await cmd_poll(message, make_config(), poller)
        assert "не удался" in message.last


@pytest.mark.asyncio
async def test_poll_without_poller_is_reported(tmp_path):
    message = FakeMessage()
    await cmd_poll(message, make_config(), None)
    assert "недоступен" in message.last


@pytest.mark.asyncio
async def test_non_admin_cannot_add_source_when_closed(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage(user_id=999)
        await cmd_addsource(
            message,
            FakeCommand("addsource", "rss https://x/feed"),
            make_config(open_commands=False),
            db,
        )
        assert "только администратору" in message.last
        assert await db.list_sources() == []


@pytest.mark.asyncio
async def test_settings_text_includes_sources_and_polling(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        text = await _settings_text(make_config(), db)
        assert "tg_public:orders" in text
        assert "Опрос источников" in text


class FakeScanPoller:
    """Records scan requests instead of touching the network."""

    def __init__(self) -> None:
        self.scanned: list[str] = []

    def scan_backlog_soon(self, keyword: str) -> None:
        self.scanned.append(keyword)


@pytest.mark.asyncio
async def test_addkeyword_triggers_an_immediate_scan(tmp_path):
    """Adding a keyword must pay off now, not at the next scheduled cycle."""
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        poller = FakeScanPoller()
        message = FakeMessage()
        await cmd_addkeyword(
            message, FakeCommand("addkeyword", "нужен логотип"), make_config(), db, poller
        )
        assert "✅" in message.last
        assert poller.scanned == ["нужен логотип"]
        assert "Ищу подходящие" in message.last
        assert await db.list_keywords() == ["нужен логотип"]


@pytest.mark.asyncio
async def test_addkeyword_escapes_html_in_the_reply(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addkeyword(
            message,
            FakeCommand("addkeyword", "<b>сайт</b>"),
            make_config(),
            db,
            FakeScanPoller(),
        )
        assert "&lt;b&gt;сайт&lt;/b&gt;" in message.last
        assert "<b>сайт</b>" not in message.last


@pytest.mark.asyncio
async def test_addkeyword_without_poller_still_stores_the_word(tmp_path):
    """No poller (e.g. sources disabled) must not break the command."""
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage()
        await cmd_addkeyword(
            message, FakeCommand("addkeyword", "сайт"), make_config(), db, None
        )
        assert "✅" in message.last
        assert "Ищу подходящие" not in message.last
        assert await db.list_keywords() == ["сайт"]


@pytest.mark.asyncio
async def test_addkeyword_does_not_scan_when_sources_disabled(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        poller = FakeScanPoller()
        message = FakeMessage()
        await cmd_addkeyword(
            message,
            FakeCommand("addkeyword", "сайт"),
            make_config(poll_sources=False),
            db,
            poller,
        )
        assert poller.scanned == []


@pytest.mark.asyncio
async def test_addkeyword_rejects_an_empty_keyword(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        poller = FakeScanPoller()
        message = FakeMessage()
        await cmd_addkeyword(
            message, FakeCommand("addkeyword", "   "), make_config(), db, poller
        )
        assert "Использование" in message.last
        assert poller.scanned == []
        assert await db.list_keywords() == []


@pytest.mark.asyncio
async def test_non_admin_cannot_add_keyword_when_closed(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        poller = FakeScanPoller()
        message = FakeMessage(user_id=999)
        await cmd_addkeyword(
            message,
            FakeCommand("addkeyword", "сайт"),
            make_config(open_commands=False),
            db,
            poller,
        )
        assert "только администратору" in message.last
        assert poller.scanned == []
        assert await db.list_keywords() == []
