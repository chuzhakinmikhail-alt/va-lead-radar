"""Optional userbot source: read and search ordinary Telegram chats.

This is the only way to reach *ordinary* group chats. The Bot API cannot see any
chat the bot is not a member of, and public-channel previews only cover
channels. A userbot logs in as a real account (MTProto), so it can read history
and run global search across public chats.

It is deliberately opt-in and off by default:

* it needs ``TELEGRAM_API_ID``, ``TELEGRAM_API_HASH`` and a ``TELEGRAM_SESSION``
  string from an account you control,
* automating a user account can get that account limited or banned, so it is
  never enabled unless those variables are present.

When the credentials are missing the source reports itself as unavailable and
the poller simply skips it, so the rest of the bot keeps working.
"""

from __future__ import annotations

import logging

from .base import LeadItem, Source

logger = logging.getLogger(__name__)

# How many recent messages to pull per chat, and the cap on global search hits.
HISTORY_LIMIT = 50
SEARCH_LIMIT = 50


class UserbotUnavailable(RuntimeError):
    """Raised when the userbot is used without usable credentials."""


class TelegramUserbotSource(Source):
    """Reads recent messages from a chat (or runs a global search) as a user."""

    kind = "userbot"

    def __init__(self, target: str, title: str = "", session=None) -> None:
        super().__init__(target, title)
        self._session = session

    @staticmethod
    def is_search_target(target: str) -> bool:
        """``userbot_search:запрос`` searches globally instead of one chat."""
        return target.strip().lower().startswith("search:")

    async def fetch(self, client) -> list[LeadItem]:  # noqa: ARG002 - API symmetry
        if self._session is None:
            logger.warning("Userbot source %s skipped: no session", self.source_id)
            return []
        try:
            if self.is_search_target(self.target):
                query = self.target.split(":", 1)[1].strip()
                return await self._search(query)
            return await self._history()
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the poll
            logger.warning("Userbot fetch failed for %s: %s", self.source_id, exc)
            return []

    async def _history(self) -> list[LeadItem]:
        from telethon.tl.types import Message

        target = self.target.lstrip("@")
        items: list[LeadItem] = []
        async for message in self._session.iter_messages(target, limit=HISTORY_LIMIT):
            if not isinstance(message, Message) or not message.message:
                continue
            items.append(self._to_item(message, target))
        return items

    async def _search(self, query: str) -> list[LeadItem]:
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, Message

        result = await self._session(
            SearchGlobalRequest(
                q=query,
                filter=InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=None,
                offset_id=0,
                limit=SEARCH_LIMIT,
            )
        )
        items: list[LeadItem] = []
        for message in getattr(result, "messages", []):
            if not isinstance(message, Message) or not message.message:
                continue
            items.append(self._to_item(message, f"search:{query}"))
        return items

    def _to_item(self, message, target: str) -> LeadItem:
        chat = getattr(message, "chat", None)
        username = getattr(chat, "username", None) if chat else None
        if username:
            url = f"https://t.me/{username}/{message.id}"
            label = username
        else:
            # Private chats have no public URL; the id is all we can offer.
            url = ""
            label = str(getattr(message, "chat_id", "") or "")
        return LeadItem(
            source_id=self.source_id,
            kind="userbot",
            external_id=f"{message.chat_id}:{message.id}",
            title=message.message.split("\n", 1)[0][:120],
            text=message.message,
            url=url,
            published=message.date.isoformat() if message.date else "",
            author=label,
        )


async def create_userbot_session(api_id: int, api_hash: str, session_string: str):
    """Build a Telethon client from a saved session string.

    Returns ``None`` when Telethon is not installed or the login fails, so the
    caller can carry on without the userbot.
    """
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.warning(
            "Telethon is not installed; userbot sources stay disabled. "
            "Install it with: pip install telethon"
        )
        return None

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(
                "TELEGRAM_SESSION is not authorised; userbot sources disabled."
            )
            await client.disconnect()
            return None
    except Exception as exc:  # noqa: BLE001 - optional feature, must not be fatal
        logger.error("Could not start the userbot session: %s", exc)
        return None

    logger.info("Userbot session started")
    return client
