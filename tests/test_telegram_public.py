"""Tests for the public Telegram channel preview parser."""

import httpx
import pytest

from app.sources.telegram_public import TelegramPublicSource, parse_channel_page

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


@pytest.mark.asyncio
async def test_channel_source_rejects_private_targets():
    source = TelegramPublicSource("+AbCdEf123")
    async with httpx.AsyncClient() as client:
        assert await source.fetch(client) == []
