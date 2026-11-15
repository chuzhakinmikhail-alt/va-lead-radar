"""End-to-end tests for the source poller.

The LLM is replaced by a scripted stand-in and the sources by fixtures, so the
real keyword matching, deduplication, storage and card rendering all run.
"""

import pytest

from app.analyzer import LeadVerdict
from app.config import Config
from app.db import SQLiteDatabase
from app.poller import Poller, format_source_card, item_hash
from app.sources import LeadItem

CHANNEL = "tg_public:orders"


class FakeAnalyzer:
    """Returns a scripted verdict and records what it was asked to classify."""

    def __init__(self, is_lead: bool = True, summary: str = "нужен бот") -> None:
        self.is_lead = is_lead
        self.summary = summary
        self.seen: list[str] = []

    async def analyze(self, text: str) -> LeadVerdict:
        self.seen.append(text)
        return LeadVerdict(is_lead=self.is_lead, summary=self.summary, intent="заказ")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="1:abc",
        admin_id=42,
        llm_api_key="k",
        poll_interval=60,
    )
    base.update(overrides)
    return Config(**base)


def make_item(
    external_id: str = "orders/1",
    text: str = "Ищу разработчика на Python, пишите @dev_ivan",
    **overrides,
) -> LeadItem:
    base = dict(
        source_id=CHANNEL,
        kind="tg_public",
        external_id=external_id,
        title=text.split("\n")[0][:120],
        text=text,
        url=f"https://t.me/{external_id}",
        author="orders",
    )
    base.update(overrides)
    return LeadItem(**base)


def _poller(db, analyzer, bot, config=None):
    return Poller(config or make_config(), db, analyzer, bot)


def test_item_hash_depends_on_source_and_id():
    a = item_hash(make_item("orders/1"))
    b = item_hash(make_item("orders/1"))
    assert a == b
    # Same id from a different source must not collide.
    other = item_hash(make_item("orders/1", source_id="rss:https://x/feed"))
    assert a != other
    assert item_hash(make_item("orders/2")) != a


@pytest.mark.asyncio
async def test_lead_with_contact_is_delivered_once(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("ищу разработчика")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)

        first = await poller._process(make_item())
        assert first == {"matched": 1, "leads": 1, "sent": 1}

        # The same item again must be suppressed by the hash, without a second
        # LLM call — this is the anti-duplicate guarantee.
        second = await poller._process(make_item())
        assert second == {"matched": 1, "leads": 0, "sent": 0}
        assert len(analyzer.seen) == 1

        assert len(bot.sent) == 1
        admin_id, card = bot.sent[0]
        assert admin_id == 42
        assert "@dev_ivan" in card
        assert "t.me/orders/1" in card

        stats = await db.stats()
        assert stats["leads"] == 1
        assert stats["analysed"] == 1


@pytest.mark.asyncio
async def test_item_without_keyword_is_ignored(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("нужен бот")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)

        result = await poller._process(make_item(text="Продаю ноутбук, недорого"))
        assert result == {"matched": 0, "leads": 0, "sent": 0}
        assert analyzer.seen == []
        assert bot.sent == []


@pytest.mark.asyncio
async def test_spam_verdict_is_recorded_but_not_sent(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("нужен сайт")
        analyzer, bot = FakeAnalyzer(is_lead=False), FakeBot()
        poller = _poller(db, analyzer, bot)

        result = await poller._process(make_item(text="Нужен сайт? Скидка 50%!"))
        assert result == {"matched": 1, "leads": 0, "sent": 0}
        assert bot.sent == []
        # Recorded so a resend is impossible, but not counted as a lead.
        stats = await db.stats()
        assert stats["analysed"] == 1
        assert stats["leads"] == 0


@pytest.mark.asyncio
async def test_lead_without_contacts_falls_back_to_url(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("нужен бот")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)

        item = make_item(text="Нужен бот для магазина, детали по ссылке")
        await poller._process(item)
        _, card = bot.sent[0]
        assert "не указаны" in card
        assert "t.me/orders/1" in card


@pytest.mark.asyncio
async def test_missing_admin_id_records_but_does_not_send(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_keyword("нужен бот")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot, config=make_config(admin_id=0))

        result = await poller._process(make_item(text="Нужен бот"))
        assert result == {"matched": 1, "leads": 1, "sent": 0}
        assert bot.sent == []
        assert (await db.stats())["leads"] == 1


@pytest.mark.asyncio
async def test_run_once_skips_when_disabled(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        poller = _poller(
            db, FakeAnalyzer(), FakeBot(), config=make_config(poll_sources=False)
        )
        assert await poller.run_once() == {
            "sources": 0,
            "items": 0,
            "matched": 0,
            "leads": 0,
            "sent": 0,
        }


@pytest.mark.asyncio
async def test_run_once_skips_paused_sources(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        await db.set_source_paused(CHANNEL, True)
        poller = _poller(db, FakeAnalyzer(), FakeBot())
        assert (await poller.run_once())["sources"] == 0


def test_card_renders_source_contacts_and_link():
    item = make_item()
    verdict = LeadVerdict(is_lead=True, summary="нужен бот", intent="заказ")
    card = format_source_card(
        item, verdict, ["ищу разработчика"], ["Telegram: @dev_ivan"], item.url
    )
    assert "с внешнего источника" in card
    assert "Telegram: @dev_ivan" in card
    assert 'href="https://t.me/orders/1"' in card
    assert "нужен бот" in card


def test_card_escapes_html_in_source_text():
    item = make_item(text="Нужен бот <script>alert(1)</script>")
    verdict = LeadVerdict(is_lead=True, summary="s", intent="i")
    card = format_source_card(item, verdict, [], [], item.url)
    assert "<script>" not in card
    assert "&lt;script&gt;" in card


class FakeSource:
    """Stands in for a real source so a scan needs no network."""

    def __init__(self, items):
        self._items = items
        self.fetches = 0

    async def fetch(self, client):
        self.fetches += 1
        return self._items


def _patch_source(monkeypatch, items):
    """Point the poller at a fake source and return it."""
    fake = FakeSource(items)
    monkeypatch.setattr(
        "app.poller.create_source", lambda *a, **kw: fake
    )
    return fake


@pytest.mark.asyncio
async def test_scan_backlog_sends_matches_for_the_new_keyword(tmp_path, monkeypatch):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)
        _patch_source(
            monkeypatch,
            [
                make_item("orders/1", text="Нужен логотип, пишите @dev_ivan"),
                make_item("orders/2", text="Продаю ноутбук"),
            ],
        )

        stats = await poller.scan_backlog("логотип")
        assert stats["sources"] == 1
        assert stats["items"] == 2
        assert stats["matched"] == 1
        assert stats["leads"] == 1
        assert stats["sent"] == 1
        assert len(bot.sent) == 1
        assert "@dev_ivan" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_scan_backlog_ignores_items_already_classified(tmp_path, monkeypatch):
    """A second keyword overlapping the first must not replay old cards."""
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)
        _patch_source(
            monkeypatch, [make_item("orders/1", text="Нужен логотип и сайт")]
        )

        first = await poller.scan_backlog("логотип")
        assert first["sent"] == 1

        second = await poller.scan_backlog("сайт")
        assert second["matched"] == 1
        assert second["leads"] == 0
        assert second["sent"] == 0
        assert len(bot.sent) == 1
        # The duplicate is caught before the LLM, so it is never re-classified.
        assert len(analyzer.seen) == 1


@pytest.mark.asyncio
async def test_scan_backlog_respects_the_item_cap(tmp_path, monkeypatch):
    from app.poller import MAX_BACKLOG

    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        poller = _poller(db, FakeAnalyzer(), FakeBot())
        items = [
            make_item(f"orders/{i}", text="Нужен сайт под ключ")
            for i in range(MAX_BACKLOG + 5)
        ]
        _patch_source(monkeypatch, items)

        stats = await poller.scan_backlog("сайт")
        assert stats["matched"] == MAX_BACKLOG
        assert stats["leads"] == MAX_BACKLOG


@pytest.mark.asyncio
async def test_scan_backlog_skips_when_polling_disabled(tmp_path, monkeypatch):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        poller = _poller(
            db, FakeAnalyzer(), FakeBot(), config=make_config(poll_sources=False)
        )
        fake = _patch_source(monkeypatch, [make_item()])
        assert await poller.scan_backlog("ищу разработчика") == {
            "sources": 0,
            "items": 0,
            "matched": 0,
            "leads": 0,
            "sent": 0,
        }
        assert fake.fetches == 0


@pytest.mark.asyncio
async def test_scan_backlog_without_sources_does_nothing(tmp_path, monkeypatch):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        poller = _poller(db, FakeAnalyzer(), FakeBot())
        fake = _patch_source(monkeypatch, [make_item()])
        assert (await poller.scan_backlog("сайт"))["sources"] == 0
        assert fake.fetches == 0


@pytest.mark.asyncio
async def test_scan_backlog_soon_runs_in_background(tmp_path, monkeypatch):
    import asyncio

    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_source("tg_public", "orders", "Orders")
        analyzer, bot = FakeAnalyzer(), FakeBot()
        poller = _poller(db, analyzer, bot)
        _patch_source(monkeypatch, [make_item(text="Нужен логотип")])

        poller.scan_backlog_soon("логотип")
        assert poller._scan_task is not None
        await poller._scan_task
        assert len(bot.sent) == 1
