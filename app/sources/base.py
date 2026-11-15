"""Pluggable lead sources.

Each source knows how to fetch a list of :class:`LeadItem` from somewhere the
bot cannot reach as a chat member: a freelance board's RSS feed, a public
Telegram channel's web preview, or — when credentials are configured — a
Telegram account search.

A source is addressed by a stable ``source_id`` of the form ``kind:target``,
which also becomes the anti-duplicate prefix for every item it yields.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

# Sent to every source. Some boards answer 403 to the default httpx agent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

FETCH_TIMEOUT = 25.0


def parse_published(value: str) -> datetime | None:
    """Parse a posting timestamp, or return ``None`` if it is unusable.

    Sources disagree on the format: RSS uses RFC 2822 ("Sun, 15 Nov 2026
    14:01:28 -0500"), Telegram's web preview uses ISO 8601. Naive stamps are
    read as UTC.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


@dataclass(frozen=True)
class LeadItem:
    """One candidate posting, normalised across all source kinds."""

    source_id: str
    kind: str
    external_id: str
    title: str
    text: str
    url: str = ""
    published: str = ""
    author: str = ""

    def is_fresh(self, max_age_days: int, now: datetime | None = None) -> bool:
        """Whether the posting is recent enough to be worth a card.

        A feed that has stopped still serves its last post forever, so without
        this a three-year-old order would be reported as a fresh lead the first
        time the source is polled. Items whose date cannot be parsed are kept:
        a source using an unusual format must not go silent. A non-positive
        limit disables the check.
        """
        if max_age_days <= 0:
            return True
        stamp = parse_published(self.published)
        if stamp is None:
            return True
        now = now or datetime.now(timezone.utc)
        return stamp >= now - timedelta(days=max_age_days)

    @property
    def haystack(self) -> str:
        """Text searched for keywords and sent to the LLM.

        Some sources derive the title from the body, so the title is only
        prepended when it adds something.
        """
        title, text = self.title.strip(), self.text.strip()
        if not text:
            return title
        if not title or title in text:
            return text
        return f"{title}\n{text}"

    @property
    def label(self) -> str:
        """Short human-readable origin, e.g. ``freelancer.com``."""
        return self.source_id.split(":", 1)[-1]


def make_source_id(kind: str, target: str) -> str:
    return f"{kind}:{target.strip()}"


class Source(ABC):
    """Base class for anything that can produce :class:`LeadItem` objects."""

    kind: str = ""

    def __init__(self, target: str, title: str = "") -> None:
        self.target = target.strip()
        self.title = title.strip()
        self.source_id = make_source_id(self.kind, self.target)

    @property
    def display(self) -> str:
        """Name shown to the admin."""
        return self.title or self.target

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[LeadItem]:
        """Return the newest items. Must not raise; log and return ``[]``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.source_id}>"


def build_client() -> httpx.AsyncClient:
    """Shared HTTP client for all sources."""
    return httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en"},
    )
