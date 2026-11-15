"""Contact extraction from listing and message text.

A lead is only useful when the admin can reply, so every card carries whatever
contact the author left behind: a Telegram handle or link, an email, a phone
number, or — when none of those exist — the URL of the original posting.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# Telegram handles. The lookbehind rejects the local part of an email
# (``user@example.com`` must not yield the handle ``example``).
_TELEGRAM_HANDLE = re.compile(r"(?<![\w.@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
_TELEGRAM_LINK = re.compile(
    r"(?:https?://)?t\.me/(?:joinchat/)?([A-Za-z0-9_+\-]{4,64})", re.IGNORECASE
)
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]{2,}")
# Phone numbers, Russian or international, with optional separators.
# Russian numbers are +7 / 8 followed by 10 digits in 3-3-2-2 groups; the
# international branch covers a country code plus 6-14 more digits.
_PHONE = re.compile(
    r"(?<![\d.])"
    r"(?:\+7|\b8)[\s\-().]{0,2}\d{3}[\s\-().]{0,2}\d{3}[\s\-().]{0,2}\d{2}"
    r"[\s\-().]{0,2}\d{2}\b"
    r"|(?<![\d.])\+\d{1,3}[\s\-().]{0,2}\d{2,4}[\s\-().]{0,2}\d{2,4}"
    r"[\s\-().]{0,2}\d{2,4}\b"
)

# Handles that appear in boilerplate rather than as a contact.
_IGNORED_HANDLES = {
    "channel", "chat", "joinchat", "share", "telegram", "username", "example",
}

MAX_CONTACTS = 5


@dataclass(frozen=True)
class Contacts:
    """Contacts found in a piece of text, deduplicated and ordered by kind."""

    telegram: list[str] = field(default_factory=list)
    email: list[str] = field(default_factory=list)
    phone: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.telegram or self.email or self.phone)

    def lines(self) -> list[str]:
        """Human-readable ``kind: value`` lines for a card."""
        out: list[str] = []
        if self.telegram:
            out.append("Telegram: " + ", ".join(self.telegram))
        if self.email:
            out.append("Email: " + ", ".join(self.email))
        if self.phone:
            out.append("Телефон: " + ", ".join(self.phone))
        return out

    def as_text(self) -> str:
        """Single-line summary, used for the ``contact`` column."""
        return " | ".join(self.lines())


def _dedupe(items: list[str], limit: int = MAX_CONTACTS) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen[:limit]


def extract_contacts(text: str) -> Contacts:
    """Pull Telegram, email and phone contacts out of ``text``."""
    if not text:
        return Contacts()

    # Entities arrive HTML-escaped from feeds; decode so "&commat;" is seen.
    plain = html.unescape(text)

    handles = [
        m.group(1)
        for m in _TELEGRAM_HANDLE.finditer(plain)
        if m.group(1).lower() not in _IGNORED_HANDLES
    ]
    links = [
        m.group(1)
        for m in _TELEGRAM_LINK.finditer(plain)
        if m.group(1).lower() not in _IGNORED_HANDLES
    ]
    telegram = _dedupe([f"@{name}" for name in handles + links])

    emails = _dedupe([m.group(0) for m in _EMAIL.finditer(plain)])
    phones = _dedupe(
        [re.sub(r"\s+", " ", m.group(0)).strip() for m in _PHONE.finditer(plain)]
    )

    return Contacts(telegram=telegram, email=emails, phone=phones)
