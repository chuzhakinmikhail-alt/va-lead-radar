"""Background poller: watch external sources and report leads.

The bot no longer has to be *added* anywhere. Every ``POLL_INTERVAL`` seconds the
poller asks each configured source for its newest items, keeps those matching a
keyword, asks the LLM whether each one is a real request, and sends a card with
whatever contact details the posting contains.

Items are deduplicated by a hash of ``source_id + external_id``, so a feed that
repeats its whole history on every request produces each lead exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .analyzer import LeadAnalyzer
from .config import Config
from .contacts import extract_contacts
from .db import Database
from .sources import LeadItem, build_client, create_source

logger = logging.getLogger(__name__)

# A single poll cycle must not run forever; feeds occasionally hang.
CYCLE_TIMEOUT = 300.0


def item_hash(item: LeadItem) -> str:
    """Stable anti-duplicate key for an external item.

    ``external_id`` alone is not enough: two boards can use the same numeric id,
    so the source is part of the key.
    """
    raw = f"{item.source_id}:{item.external_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Poller:
    """Fetches configured sources and forwards new leads to the admin."""

    def __init__(
        self,
        config: Config,
        db: Database,
        analyzer: LeadAnalyzer,
        bot: Bot,
        userbot_session=None,
    ) -> None:
        self._config = config
        self._db = db
        self._analyzer = analyzer
        self._bot = bot
        self._userbot_session = userbot_session
        self._task: asyncio.Task[None] | None = None
        self._client = None

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Poller started (interval %ss, sources enabled: %s)",
            self._config.poll_interval,
            self._config.poll_sources,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("Poller stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.run_once(), timeout=CYCLE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Poll cycle exceeded %.0fs and was abandoned", CYCLE_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.exception("Poll cycle failed: %s", exc)
            await asyncio.sleep(self._config.poll_interval)

    # ---- one cycle -----------------------------------------------------
    async def run_once(self) -> dict[str, int]:
        """Poll every active source once. Returns per-run counters."""
        stats = {"sources": 0, "items": 0, "matched": 0, "leads": 0, "sent": 0}
        if not self._config.poll_sources:
            logger.debug("Source polling is disabled (POLL_SOURCES=false)")
            return stats

        rows = await self._db.list_sources(include_paused=False)
        if not rows:
            logger.info("No active sources configured; nothing to poll")
            return stats

        if self._client is None:
            self._client = build_client()

        for row in rows:
            source = create_source(
                row.kind, row.target, row.title, userbot_session=self._userbot_session
            )
            if source is None:
                logger.warning("Unknown source kind %r; skipped", row.kind)
                continue

            stats["sources"] += 1
            try:
                items = await source.fetch(self._client)
            except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
                logger.warning("Source %s failed: %s", row.source_id, exc)
                continue

            stats["items"] += len(items)
            for item in items:
                try:
                    outcome = await self._process(item)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to process item from %s: %s", row.source_id, exc)
                    continue
                stats["matched"] += outcome["matched"]
                stats["leads"] += outcome["leads"]
                stats["sent"] += outcome["sent"]

        logger.info(
            "Poll done: %s source(s), %s item(s), %s matched, %s lead(s), %s sent",
            stats["sources"],
            stats["items"],
            stats["matched"],
            stats["leads"],
            stats["sent"],
        )
        return stats

    async def _process(self, item: LeadItem) -> dict[str, int]:
        result = {"matched": 0, "leads": 0, "sent": 0}

        text = item.haystack
        if not text:
            return result

        matched = await self._db.match_keywords(text)
        if not matched:
            return result
        result["matched"] = 1

        digest = item_hash(item)
        if await self._db.is_duplicate(digest):
            logger.debug("Duplicate source item skipped: %s", digest)
            return result

        verdict = await self._analyzer.analyze(text)

        contacts = extract_contacts(text)
        # Without contacts the card still needs somewhere to point, so fall back
        # to the posting's own URL, then to the source label.
        contact = contacts.as_text()
        if not contact and item.url:
            contact = f"Ссылка: {item.url}"

        newly = await self._db.record_lead(
            msg_hash=digest,
            chat_id=0,
            message_id=0,
            is_lead=verdict.is_lead,
            summary=verdict.summary,
            intent=verdict.intent,
            source=item.source_id,
            url=item.url,
            author=item.author,
            contact=contact,
        )
        if not newly:
            return result

        if not verdict.is_lead:
            logger.info("Not a lead (%s): %s", item.source_id, matched)
            return result

        result["leads"] = 1
        if await self._deliver(item, verdict, matched, contacts.lines(), item.url):
            result["sent"] = 1
        return result

    async def _deliver(
        self,
        item: LeadItem,
        verdict,
        matched: list[str],
        contact_lines: list[str],
        url: str,
    ) -> bool:
        if not self._config.admin_id:
            logger.warning(
                "Lead found in %s but ADMIN_ID is not set; card not delivered",
                item.source_id,
            )
            return False

        card = format_source_card(item, verdict, matched, contact_lines, url)
        try:
            await self._bot.send_message(
                self._config.admin_id, card, disable_web_page_preview=True
            )
        except TelegramAPIError as exc:
            logger.error("Could not send source lead card: %s", exc)
            return False
        logger.info(
            "Source lead sent (%s, intent=%r)", item.source_id, verdict.intent
        )
        return True


def format_source_card(
    item: LeadItem,
    verdict,
    matched: list[str],
    contact_lines: list[str],
    url: str,
) -> str:
    """Render a lead found on an external source."""
    lines = [
        "🌐 <b>Лид с внешнего источника</b>",
        "",
        f"<b>Источник:</b> {html.escape(item.label)}",
    ]
    if item.author:
        lines.append(f"<b>Автор:</b> {html.escape(item.author)}")
    if item.published:
        lines.append(f"<b>Опубликовано:</b> {html.escape(item.published)}")
    lines.append(f"<b>Интент:</b> {html.escape(verdict.intent)}")
    lines.append(f"<b>Саммари:</b> {html.escape(verdict.summary)}")
    if matched:
        lines.append(f"<b>Совпало:</b> {html.escape(', '.join(matched))}")

    if contact_lines:
        lines.append("<b>Контакты:</b>")
        for line in contact_lines:
            lines.append(f"• {html.escape(line)}")
    elif url:
        lines.append("<b>Контакты:</b> не указаны — смотрите объявление")

    if url:
        lines.append(
            f'<b>Ссылка:</b> <a href="{html.escape(url, quote=True)}">'
            "открыть объявление</a>"
        )

    lines.append("")
    lines.append("<b>Текст:</b>")
    body = item.haystack[:1000]
    lines.append(f"<blockquote>{html.escape(body)}</blockquote>")
    return "\n".join(lines)


__all__ = ["Poller", "item_hash", "format_source_card"]
