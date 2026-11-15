"""Source registry and factory.

``create_source(kind, target, title, userbot_session)`` turns a stored row into
a live source object. Kinds are also listed here with their defaults, which is
what ``/sources`` and the seeding logic read.
"""

from __future__ import annotations

from .base import LeadItem, Source, build_client, make_source_id
from .rss import RssSource, parse_feed, strip_html
from .telegram_public import (
    TelegramPublicSource,
    is_valid_channel,
    normalise_channel,
    parse_channel_page,
)
from .telegram_userbot import (
    TelegramUserbotSource,
    UserbotUnavailable,
    create_userbot_session,
)

__all__ = [
    "LeadItem",
    "Source",
    "build_client",
    "make_source_id",
    "RssSource",
    "parse_feed",
    "strip_html",
    "TelegramPublicSource",
    "normalise_channel",
    "is_valid_channel",
    "parse_channel_page",
    "TelegramUserbotSource",
    "UserbotUnavailable",
    "create_userbot_session",
    "SOURCE_KINDS",
    "create_source",
    "kind_help",
]

# Kind -> how the admin should write the target.
SOURCE_KINDS: dict[str, str] = {
    "rss": "URL RSS-ленты биржи или доски заказов",
    "tg_public": "публичный канал: @name, name или ссылка t.me/name",
    "userbot": "чат (@name или id), либо search:<запрос> — нужен userbot",
}

DEFAULT_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("rss", "https://www.freelancer.com/rss.xml", "Freelancer.com"),
    ("rss", "https://weworkremotely.com/categories/remote-programming-jobs.rss",
     "We Work Remotely"),
    ("tg_public", "freelance_zakazy", "TG: freelance_zakazy"),
    ("tg_public", "zakazy_it", "TG: zakazy_it"),
)


def create_source(
    kind: str, target: str, title: str = "", userbot_session=None
) -> Source | None:
    """Build a source object, or ``None`` when the kind is unknown."""
    kind = (kind or "").strip().lower()
    if kind == "rss":
        return RssSource(target, title)
    if kind == "tg_public":
        return TelegramPublicSource(target, title)
    if kind == "userbot":
        return TelegramUserbotSource(target, title, session=userbot_session)
    return None


def kind_help() -> str:
    """Human-readable list of source kinds for the help text."""
    return "\n".join(f"• <code>{kind}</code> — {help_}" for kind, help_ in SOURCE_KINDS.items())
