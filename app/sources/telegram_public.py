"""Public Telegram channel source, read through the ``t.me/s/`` web preview.

The Bot API cannot read a chat the bot is not a member of, and joining hundreds
of chats is exactly what we want to avoid. Public channels, however, publish a
server-rendered preview at ``https://t.me/s/<username>`` that needs no login,
so the bot can watch them without being added anywhere.

Limitations, worth knowing before adding a channel:

* only *public* channels with a username work; private groups and invite links
  are not reachable this way,
* the preview shows roughly the last 20 posts, so polling must be frequent
  enough that nothing scrolls past between two runs.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from .base import LeadItem, Source
from .rss import strip_html

logger = logging.getLogger(__name__)

# One message block starts at its data-post attribute.
_POST_SPLIT = re.compile(r'data-post="([^"]+)"')
_TEXT_DIV = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
_AUTHOR = re.compile(
    r'class="tgme_widget_message_from_author"[^>]*>(.*?)</div>', re.DOTALL
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_POST_ID_RE = re.compile(r"/(\d+)$")

# The preview shows ~20 posts per page. On a free host the instance sleeps, so
# two polls can be hours apart and a busy channel would scroll its older orders
# out of the window. Walking back a few pages closes that gap; the anti-duplicate
# hash makes the overlap between runs harmless.
DEFAULT_HISTORY_PAGES = 3


def oldest_post_id(items: list[LeadItem]) -> int | None:
    """Lowest message id on a page, which is the ``before`` cursor for the next."""
    ids = [int(m.group(1)) for i in items if (m := _POST_ID_RE.search(i.external_id))]
    return min(ids) if ids else None


def normalise_channel(target: str) -> str:
    """Accept ``name``, ``@name`` or a ``t.me`` URL; return the bare name."""
    value = (target or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?t\.me/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^s/", "", value)
    value = value.strip("/").lstrip("@")
    return value.split("/")[0].split("?")[0]


def is_valid_channel(name: str) -> bool:
    return bool(_USERNAME_RE.match(name))


def parse_channel_page(page: str, channel: str) -> list[LeadItem]:
    """Extract posts from a ``t.me/s/<channel>`` page.

    Pure function so it can be tested against a saved page.
    """
    if not page:
        return []

    matches = list(_POST_SPLIT.finditer(page))
    items: list[LeadItem] = []

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(page)
        block = page[start:end]

        text_match = _TEXT_DIV.search(block)
        if not text_match:
            continue
        text = strip_html(text_match.group(1))
        if not text:
            continue

        post_ref = match.group(1)  # e.g. "channel/123"
        message_id = post_ref.rsplit("/", 1)[-1]
        url = f"https://t.me/{post_ref}"

        time_match = _TIME.search(block)
        author_match = _AUTHOR.search(block)
        author = strip_html(author_match.group(1)) if author_match else ""

        items.append(
            LeadItem(
                source_id=f"tg_public:{channel}",
                kind="tg_public",
                external_id=post_ref,
                title=text.split("\n", 1)[0][:120],
                text=text,
                url=url,
                published=time_match.group(1) if time_match else "",
                author=author,
            )
        )

    # The preview lists oldest first; newest first is what the admin wants.
    items.reverse()
    return items


class TelegramPublicSource(Source):
    """A public Telegram channel, watched through its web preview."""

    kind = "tg_public"

    def __init__(self, target: str, title: str = "", history_pages: int = 1) -> None:
        super().__init__(normalise_channel(target), title)
        # 1 page is the live preview window; more pages reach further back, which
        # matters when the host sleeps between polls.
        self.history_pages = max(1, int(history_pages))

    async def _fetch_page(self, client: httpx.AsyncClient, before: int | None) -> list[LeadItem]:
        url = f"https://t.me/s/{self.target}"
        if before is not None:
            url = f"{url}?before={before}"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Telegram preview fetch failed for %s: %s", url, exc)
            return []
        return parse_channel_page(resp.text, self.target)

    async def fetch(self, client: httpx.AsyncClient) -> list[LeadItem]:
        if not is_valid_channel(self.target):
            logger.warning("Not a usable public channel name: %r", self.target)
            return []

        collected: dict[str, LeadItem] = {}
        before: int | None = None

        for page in range(self.history_pages):
            items = await self._fetch_page(client, before)
            if not items:
                break
            for item in items:
                collected.setdefault(item.external_id, item)
            cursor = oldest_post_id(items)
            # A page that repeats the previous one means the history is exhausted.
            if cursor is None or cursor == before:
                break
            before = cursor

        items = list(collected.values())
        if not items:
            logger.info(
                "Telegram channel %s yielded no posts (private, empty or renamed)",
                self.target,
            )
        else:
            logger.info(
                "Telegram channel %s: %d post(s) over %d page(s)",
                self.target,
                len(items),
                min(page + 1, self.history_pages),
            )
        return items
