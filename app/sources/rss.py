"""RSS/Atom source: freelance boards and job feeds.

Boards publish their newest orders as RSS, which is the only access they grant
without an API key. The feed URL is the source target, so adding a board is
``/addsource rss https://example.com/feed.xml``.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from .base import LeadItem, Source

logger = logging.getLogger(__name__)

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def strip_html(raw: str) -> str:
    """Turn a feed's HTML payload into readable plain text."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li)>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _child_text(node: ET.Element, *names: str) -> str:
    """First non-empty child text among ``names`` (namespace-agnostic).

    Atom nests the author as ``<author><name>…</name></author>``, so when a
    match has no direct text its ``name`` child is consulted as well.
    """
    for name in names:
        for tag in (name, f"{_ATOM_NS}{name}"):
            found = node.find(tag)
            if found is None:
                continue
            text = (found.text or "").strip()
            if text:
                return text
            nested = found.find("name") or found.find(f"{_ATOM_NS}name")
            if nested is not None and (nested.text or "").strip():
                return nested.text.strip()
    return ""


def _link(node: ET.Element) -> str:
    """Best link for an RSS item or an Atom entry."""
    for tag in ("link", f"{_ATOM_NS}link"):
        found = node.find(tag)
        if found is not None:
            href = (found.get("href") or "").strip()
            if href:
                return href
            if found.text and found.text.strip():
                return found.text.strip()
    guid = _child_text(node, "guid", "id")
    return guid if guid.startswith("http") else ""


def parse_feed(xml_text: str, source_id: str, limit: int = 50) -> list[LeadItem]:
    """Parse an RSS or Atom document into lead items.

    Kept free of I/O so it can be unit-tested against fixtures.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Cannot parse feed %s: %s", source_id, exc)
        return []

    nodes = root.findall(".//item") or root.findall(f".//{_ATOM_NS}entry")
    items: list[LeadItem] = []

    for node in nodes[:limit]:
        title = strip_html(_child_text(node, "title"))
        body = strip_html(
            _child_text(node, "description", "summary", "content", "encoded")
        )
        link = _link(node)
        published = _child_text(node, "pubDate", "published", "updated", "date")
        author = strip_html(_child_text(node, "author", "creator", "name"))
        external_id = _child_text(node, "guid", "id") or link or title
        if not (title or body):
            continue
        items.append(
            LeadItem(
                source_id=source_id,
                kind="rss",
                external_id=external_id,
                title=title,
                text=body,
                url=link,
                published=published,
                author=author,
            )
        )
    return items


class RssSource(Source):
    """A freelance board or job feed reachable over plain HTTP."""

    kind = "rss"

    async def fetch(self, client: httpx.AsyncClient) -> list[LeadItem]:
        try:
            resp = await client.get(self.target)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("RSS fetch failed for %s: %s", self.target, exc)
            return []

        items = parse_feed(resp.text, self.source_id)
        logger.info("RSS %s: %d item(s)", self.target, len(items))
        return items
