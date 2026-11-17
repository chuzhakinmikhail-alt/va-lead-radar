"""Tests for the public Telegram channel preview parser."""

import httpx
import pytest

from app.sources.telegram_public import (
    TelegramPublicSource,
    oldest_post_id,
    parse_channel_page,
)

# Trimmed from a real t.me/s/<channel> page: two posts, the second with a link.
CHANNEL_PAGE = """
<div class="tgme_channel_history">
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="orders/101">
      <div class="tgme_widget_message_text js-message_text">
        Ищу разработчика на Python, пишите @dev_python
      </div>
      <time datetime="2025-11-10T10:00:00+00:00"></time>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="orders/102">
      <div class="tgme_widget_message_text js-message_text">
        Нужен сайт на WordPress<br/>Бюджет 60к, контакт: site@example.com
      </div>
      <time datetime="2025-11-10T11:00:00+00:00"></time>
    </div>
  </div>
</div>
"""


def test_parse_channel_page_returns_newest_first():
    items = parse_channel_page(CHANNEL_PAGE, "orders")
    assert [i.external_id for i in items] == ["orders/102", "orders/101"]
    assert items[0].url == "https://t.me/orders/102"
    assert items[0].source_id == "tg_public:orders"
    assert "site@example.com" in items[0].text


def test_parse_channel_page_ignores_service_posts():
    page = '<div class="tgme_widget_message" data-post="c/1"><div>x</div></div>'
    assert parse_channel_page(page, "c") == []


def test_parse_channel_page_handles_empty_page():
    assert parse_channel_page("", "c") == []


@pytest.mark.asyncio
async def test_channel_source_fetches_preview_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=CHANNEL_PAGE)

    source = TelegramPublicSource("@orders")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        items = await source.fetch(client)

    assert seen == ["https://t.me/s/orders"]
    assert len(items) == 2


def _page(channel: str, ids: list[int], text: str = "Ищу разработчика, пишите @dev") -> str:
    """Build a preview page for the given message ids, oldest first."""
    blocks = []
    for i in ids:
        blocks.append(
            f'<div class="tgme_widget_message" data-post="{channel}/{i}">'
            f'<div class="tgme_widget_message_text">{text}</div>'
            f'<time datetime="2025-11-10T10:00:00+00:00"></time></div>'
        )
    return "".join(blocks)


def test_oldest_post_id_is_the_pagination_cursor():
    items = parse_channel_page(_page("orders", [10, 11, 12]), "orders")
    assert oldest_post_id(items) == 10
    assert oldest_post_id([]) is None


@pytest.mark.asyncio
async def test_channel_source_walks_history_pages():
    """A long sleep must not lose posts that scrolled past page one."""
    pages = {
        None: _page("orders", [30, 31, 32]),
        30: _page("orders", [27, 28, 29]),
        27: _page("orders", [24, 25, 26]),
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        before = request.url.params.get("before")
        key = int(before) if before is not None else None
        return httpx.Response(200, text=pages.get(key, ""))

    source = TelegramPublicSource("orders", history_pages=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await source.fetch(client)

    assert seen == [
        "https://t.me/s/orders",
        "https://t.me/s/orders?before=30",
        "https://t.me/s/orders?before=27",
    ]
    ids = [i.external_id for i in items]
    assert len(ids) == 9
    assert "orders/24" in ids and "orders/32" in ids


@pytest.mark.asyncio
async def test_channel_source_stops_when_history_is_exhausted():
    """Repeated pages must not loop forever."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # Same page whatever the cursor: the channel has no older history.
        return httpx.Response(200, text=_page("orders", [5, 6, 7]))

    source = TelegramPublicSource("orders", history_pages=5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await source.fetch(client)

    assert len(calls) == 2
    assert len(items) == 3


@pytest.mark.asyncio
async def test_channel_source_single_page_by_default():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=_page("orders", [1, 2, 3]))

    source = TelegramPublicSource("orders")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await source.fetch(client)

    assert calls == ["https://t.me/s/orders"]
    assert len(items) == 3


@pytest.mark.asyncio
async def test_channel_source_rejects_private_targets():
    source = TelegramPublicSource("+AbCdEf123")
    async with httpx.AsyncClient() as client:
        assert await source.fetch(client) == []
