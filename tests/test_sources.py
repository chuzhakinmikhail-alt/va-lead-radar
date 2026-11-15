"""Tests for external sources: feed/channel parsing and the source factory."""

import httpx
import pytest

from app.sources import (
    RssSource,
    TelegramPublicSource,
    create_source,
    normalise_channel,
)
from app.sources.rss import parse_feed, strip_html
from app.sources.telegram_public import is_valid_channel, parse_channel_page

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Freelance orders</title>
    <item>
      <title>Нужен бот для Telegram</title>
      <description>&lt;p&gt;Ищу разработчика, бюджет 50к. Пишите @bot_maker&lt;/p&gt;</description>
      <link>https://example.com/orders/1</link>
      <guid>https://example.com/orders/1</guid>
      <pubDate>Mon, 10 Nov 2025 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Скидка на курсы</title>
      <description>Реклама, не заказ</description>
      <link>https://example.com/orders/2</link>
      <guid>order-2</guid>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Jobs</title>
  <entry>
    <title>Нужен сайт на WordPress</title>
    <summary>Срочно нужен сайт, бюджет обсуждаем</summary>
    <link href="https://example.com/jobs/9"/>
    <id>tag:example.com,2025:9</id>
    <updated>2025-11-11T09:00:00Z</updated>
    <author><name>Ivan</name></author>
  </entry>
</feed>
"""


def test_parse_rss_extracts_items_and_strips_html():
    items = parse_feed(RSS_SAMPLE, "rss:test")
    assert len(items) == 2
    first = items[0]
    assert first.title == "Нужен бот для Telegram"
    assert "Ищу разработчика" in first.text
    assert "<p>" not in first.text
    assert first.url == "https://example.com/orders/1"
    assert first.external_id == "https://example.com/orders/1"
    assert first.kind == "rss"


def test_parse_atom_extracts_entries():
    items = parse_feed(ATOM_SAMPLE, "rss:atom")
    assert len(items) == 1
    item = items[0]
    assert item.title == "Нужен сайт на WordPress"
    assert item.url == "https://example.com/jobs/9"
    assert item.author == "Ivan"
    assert item.published == "2025-11-11T09:00:00Z"


def test_parse_feed_rejects_broken_xml():
    assert parse_feed("<rss><channel>", "rss:bad") == []


def test_strip_html_keeps_line_breaks():
    assert strip_html("a<br/>b</p>c") == "a\nb\nc"


def test_normalise_channel_accepts_common_spellings():
    for raw in ("@name", "name", "https://t.me/name", "t.me/s/name", "t.me/name/"):
        assert normalise_channel(raw) == "name"


def test_is_valid_channel_rejects_invite_links():
    assert is_valid_channel("freelance_orders")
    assert not is_valid_channel("+AbCdEf123")
    assert not is_valid_channel("ab")


def test_create_source_builds_expected_kinds():
    assert isinstance(create_source("rss", "https://x/feed"), RssSource)
    assert isinstance(create_source("tg_public", "@chan"), TelegramPublicSource)
    assert create_source("nope", "x") is None


@pytest.mark.asyncio
async def test_rss_source_reports_http_error_as_empty():
    """A failing feed must not raise; the poller has to keep going."""
    source = RssSource("https://example.com/feed.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await source.fetch(client) == []


@pytest.mark.asyncio
async def test_rss_source_parses_live_response():
    source = RssSource("https://example.com/feed.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS_SAMPLE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        items = await source.fetch(client)
    assert len(items) == 2
    assert items[0].source_id == "rss:https://example.com/feed.xml"
