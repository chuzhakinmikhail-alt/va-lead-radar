"""Storage layer with two interchangeable backends.

* ``SQLiteDatabase`` — a local file. Fine for a laptop, a VPS or Docker.
* ``PostgresDatabase`` — Postgres (Neon, Render Postgres). Required on hosts with
  an ephemeral filesystem, where a SQLite file is wiped on every deploy.

``create_database(config)`` picks the backend from ``config.use_postgres``.

Tables
------
chats    : chats/channels being monitored. ``paused`` excludes a chat from
           monitoring without deleting it, which matters in auto mode: a deleted
           chat would simply be re-registered on its next message.
sources  : external places polled on a schedule — a freelance board's RSS feed,
           a public Telegram channel's web preview, or a userbot chat/search.
keywords : trigger keywords (case-insensitive, stored lowercased).
leads    : every analysed message; the primary key doubles as anti-duplicate
           protection so one message is never forwarded twice. Rows coming from
           ``sources`` carry ``source``, ``url``, ``author`` and ``contact`` so a
           card can always point somewhere useful.
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT    NOT NULL DEFAULT '',
    added_at   TEXT    NOT NULL,
    paused     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    source_id  TEXT    PRIMARY KEY,
    kind       TEXT    NOT NULL,
    target     TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    paused     INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    keyword    TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    msg_hash   TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    is_lead    INTEGER NOT NULL,
    summary    TEXT    NOT NULL DEFAULT '',
    intent     TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT '',
    url        TEXT    NOT NULL DEFAULT '',
    author     TEXT    NOT NULL DEFAULT '',
    contact    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads (created_at);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id    BIGINT PRIMARY KEY,
    title      TEXT   NOT NULL DEFAULT '',
    added_at   TEXT   NOT NULL,
    paused     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    source_id  TEXT    PRIMARY KEY,
    kind       TEXT    NOT NULL,
    target     TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    paused     INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    keyword    TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    msg_hash   TEXT PRIMARY KEY,
    chat_id    BIGINT NOT NULL DEFAULT 0,
    message_id BIGINT NOT NULL DEFAULT 0,
    is_lead    INTEGER NOT NULL,
    summary    TEXT   NOT NULL DEFAULT '',
    intent     TEXT   NOT NULL DEFAULT '',
    source     TEXT   NOT NULL DEFAULT '',
    url        TEXT   NOT NULL DEFAULT '',
    author     TEXT   NOT NULL DEFAULT '',
    contact    TEXT   NOT NULL DEFAULT '',
    created_at TEXT   NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads (created_at);
"""

# Columns added after the first release, applied at startup. Postgres supports
# IF NOT EXISTS; SQLite does not, so its backend inspects the table instead.
POSTGRES_MIGRATIONS = (
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS paused INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS author TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact TEXT NOT NULL DEFAULT ''",
)

# SQLite equivalent of POSTGRES_MIGRATIONS: (table, column, DDL fragment).
SQLITE_MIGRATIONS = (
    ("chats", "paused", "paused INTEGER NOT NULL DEFAULT 0"),
    ("leads", "source", "source TEXT NOT NULL DEFAULT ''"),
    ("leads", "url", "url TEXT NOT NULL DEFAULT ''"),
    ("leads", "author", "author TEXT NOT NULL DEFAULT ''"),
    ("leads", "contact", "contact TEXT NOT NULL DEFAULT ''"),
)

# libpq-only query parameters that asyncpg forwards to the server as settings and
# then rejects ("unrecognized configuration parameter"). Neon adds
# `channel_binding=require` to every connection string by default.
_DSN_STRIP_PARAMS = {
    "channel_binding",
    "target_session_attrs",
    "connect_timeout",
    "sslmode",
    "application_name",
}


@dataclass(frozen=True)
class Chat:
    chat_id: int
    title: str
    paused: bool = False


@dataclass(frozen=True)
class SourceRow:
    """A stored external source (feed, public channel, userbot target)."""

    source_id: str
    kind: str
    target: str
    title: str = ""
    paused: bool = False

    @property
    def display(self) -> str:
        return self.title or self.target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def message_hash(chat_id: int, message_id: int) -> str:
    """Stable hash identifying a single message inside a chat."""
    raw = f"{chat_id}:{message_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# Word characters for keyword matching. Python's ``\b`` only knows ASCII, so
# Cyrillic needs an explicit class or "бот" would match inside "работа".
_WORD_CHARS = "0-9A-Za-z_\\u0400-\\u04ff"
_LEFT_EDGE = f"(?<![{_WORD_CHARS}])"
_RIGHT_EDGE = f"(?![{_WORD_CHARS}])"


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile a keyword into a whole-word, case-insensitive matcher.

    Matching on word boundaries rather than plain substring is what keeps a
    short keyword useful: ``бот`` must not fire on ``работа``. A keyword that
    is a phrase ("нужен бот") simply matches as a phrase, and punctuation
    inside it (``нужен сайт?``) is escaped so it stays literal.
    """
    return re.compile(_LEFT_EDGE + re.escape(keyword) + _RIGHT_EDGE, re.IGNORECASE)


def match_keyword_list(keywords: list[str], text: str) -> list[str]:
    """Return the keywords present in ``text`` as whole words.

    Kept as a free function so both backends share one implementation and the
    behaviour can be unit-tested without a database.
    """
    if not text:
        return []
    return [kw for kw in keywords if kw and keyword_pattern(kw).search(text)]


def split_ssl(dsn: str) -> tuple[str, bool]:
    """Return ``(clean_dsn, needs_ssl)`` for a Postgres connection string.

    Strips query parameters that only libpq understands (Neon's
    ``channel_binding`` above all) and reports whether SSL was requested, so the
    caller can pass ``ssl=True`` to asyncpg instead.
    """
    parts = urlsplit(dsn)
    if not parts.query:
        return dsn, False

    kept: list[tuple[str, str]] = []
    needs_ssl = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in _DSN_STRIP_PARAMS:
            if key == "sslmode" and value in {"require", "verify-ca", "verify-full"}:
                needs_ssl = True
            continue
        kept.append((key, value))

    clean = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return clean, needs_ssl


class Database(ABC):
    """Async storage interface shared by both backends."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abstractmethod
    async def migrate(self) -> None: ...

    @abstractmethod
    async def add_chat(self, chat_id: int, title: str = "") -> bool: ...

    @abstractmethod
    async def remove_chat(self, chat_id: int) -> bool: ...

    @abstractmethod
    async def list_chats(self) -> list[Chat]: ...

    @abstractmethod
    async def is_monitored(self, chat_id: int) -> bool: ...

    @abstractmethod
    async def set_paused(self, chat_id: int, paused: bool) -> bool: ...

    @abstractmethod
    async def add_keyword(self, keyword: str) -> bool: ...

    @abstractmethod
    async def remove_keyword(self, keyword: str) -> bool: ...

    @abstractmethod
    async def list_keywords(self) -> list[str]: ...

    @abstractmethod
    async def match_keywords(self, text: str) -> list[str]: ...

    @abstractmethod
    async def is_duplicate(self, msg_hash: str) -> bool: ...

    @abstractmethod
    async def record_lead(
        self,
        msg_hash: str,
        chat_id: int,
        message_id: int,
        is_lead: bool,
        summary: str = "",
        intent: str = "",
        source: str = "",
        url: str = "",
        author: str = "",
        contact: str = "",
    ) -> bool: ...

    @abstractmethod
    async def add_source(
        self, kind: str, target: str, title: str = ""
    ) -> bool: ...

    @abstractmethod
    async def remove_source(self, source_id: str) -> bool: ...

    @abstractmethod
    async def list_sources(self, include_paused: bool = True) -> list[SourceRow]: ...

    @abstractmethod
    async def get_source(self, source_id: str) -> SourceRow | None: ...

    @abstractmethod
    async def set_source_paused(self, source_id: str, paused: bool) -> bool: ...

    @abstractmethod
    async def stats(self) -> dict[str, int]: ...

    async def seed_keywords(self, keywords: tuple[str, ...] | list[str]) -> int:
        """Insert default keywords, but only when the table is empty."""
        if await self.list_keywords():
            return 0
        added = await self.add_keywords(keywords)
        if added:
            logger.info("Seeded %d default keyword(s)", added)
        return added

    async def add_keywords(self, keywords: tuple[str, ...] | list[str]) -> int:
        """Insert several keywords, ignoring the ones already present."""
        added = 0
        for keyword in keywords:
            if await self.add_keyword(keyword):
                added += 1
        return added

    async def seed_sources(
        self, sources: tuple[tuple[str, str, str], ...]
    ) -> int:
        """Insert default sources, but only when the table is empty."""
        if await self.list_sources():
            return 0
        added = 0
        for kind, target, title in sources:
            if await self.add_source(kind, target, title):
                added += 1
        if added:
            logger.info("Seeded %d default source(s)", added)
        return added


class SQLiteDatabase(Database):
    """aiosqlite-backed storage for local runs, VPS and Docker."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn = None  # type: ignore[assignment]

    async def connect(self) -> None:
        import aiosqlite

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SQLITE_SCHEMA)
        await self._conn.commit()
        await self.migrate()
        logger.info("SQLite database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def migrate(self) -> None:
        """Add columns introduced after the first release (idempotent).

        SQLite has no ``ADD COLUMN IF NOT EXISTS``, so each expected column is
        checked against ``PRAGMA table_info`` before being added.
        """
        for table, column, ddl in SQLITE_MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in await cur.fetchall()}
            if column in columns:
                continue
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            await self.conn.commit()
            logger.info("Migrated %s table: added %s column", table, column)

    async def add_chat(self, chat_id: int, title: str = "") -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title, added_at) VALUES (?, ?, ?)",
            (chat_id, title, _now()),
        )
        if cur.rowcount == 0:
            if title:
                await self.conn.execute(
                    "UPDATE chats SET title = ? WHERE chat_id = ? AND title = ''",
                    (title, chat_id),
                )
            await self.conn.commit()
            return False
        await self.conn.commit()
        return True

    async def remove_chat(self, chat_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_chats(self) -> list[Chat]:
        cur = await self.conn.execute(
            "SELECT chat_id, title, paused FROM chats ORDER BY added_at ASC"
        )
        return [
            Chat(chat_id=r["chat_id"], title=r["title"], paused=bool(r["paused"]))
            for r in await cur.fetchall()
        ]

    async def is_monitored(self, chat_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM chats WHERE chat_id = ? AND paused = 0 LIMIT 1", (chat_id,)
        )
        return await cur.fetchone() is not None

    async def set_paused(self, chat_id: int, paused: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE chats SET paused = ? WHERE chat_id = ?", (int(paused), chat_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_keyword(self, keyword: str) -> bool:
        keyword = keyword.strip().lower()
        if not keyword:
            return False
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO keywords (keyword, added_at) VALUES (?, ?)",
            (keyword, _now()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_keyword(self, keyword: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM keywords WHERE keyword = ?", (keyword.strip().lower(),)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_keywords(self) -> list[str]:
        cur = await self.conn.execute(
            "SELECT keyword FROM keywords ORDER BY added_at ASC"
        )
        return [r["keyword"] for r in await cur.fetchall()]

    async def match_keywords(self, text: str) -> list[str]:
        if not text:
            return []
        return match_keyword_list(await self.list_keywords(), text)

    async def is_duplicate(self, msg_hash: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM leads WHERE msg_hash = ? LIMIT 1", (msg_hash,)
        )
        return await cur.fetchone() is not None

    async def record_lead(
        self,
        msg_hash: str,
        chat_id: int,
        message_id: int,
        is_lead: bool,
        summary: str = "",
        intent: str = "",
        source: str = "",
        url: str = "",
        author: str = "",
        contact: str = "",
    ) -> bool:
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO leads
                (msg_hash, chat_id, message_id, is_lead, summary, intent,
                 source, url, author, contact, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_hash,
                chat_id,
                message_id,
                int(is_lead),
                summary,
                intent,
                source,
                url,
                author,
                contact,
                _now(),
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_source(self, kind: str, target: str, title: str = "") -> bool:
        kind = (kind or "").strip().lower()
        target = (target or "").strip()
        if not kind or not target:
            return False
        source_id = f"{kind}:{target}"
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO sources
                (source_id, kind, target, title, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, kind, target, title, _now()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_source(self, source_id: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM sources WHERE source_id = ?", (source_id.strip(),)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_sources(self, include_paused: bool = True) -> list[SourceRow]:
        sql = "SELECT source_id, kind, target, title, paused FROM sources"
        if not include_paused:
            sql += " WHERE paused = 0"
        sql += " ORDER BY added_at ASC"
        cur = await self.conn.execute(sql)
        return [
            SourceRow(
                source_id=r["source_id"],
                kind=r["kind"],
                target=r["target"],
                title=r["title"],
                paused=bool(r["paused"]),
            )
            for r in await cur.fetchall()
        ]

    async def get_source(self, source_id: str) -> SourceRow | None:
        cur = await self.conn.execute(
            "SELECT source_id, kind, target, title, paused FROM sources WHERE source_id = ?",
            (source_id.strip(),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return SourceRow(
            source_id=row["source_id"],
            kind=row["kind"],
            target=row["target"],
            title=row["title"],
            paused=bool(row["paused"]),
        )

    async def set_source_paused(self, source_id: str, paused: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE sources SET paused = ? WHERE source_id = ?",
            (int(paused), source_id.strip()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def stats(self) -> dict[str, int]:
        cur = await self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chats)                          AS chats,
                (SELECT COUNT(*) FROM sources)                        AS sources,
                (SELECT COUNT(*) FROM keywords)                       AS keywords,
                (SELECT COUNT(*) FROM leads)                          AS analysed,
                (SELECT COUNT(*) FROM leads WHERE is_lead = 1)        AS leads
            """
        )
        row = await cur.fetchone()
        return (
            dict(row)
            if row
            else {"chats": 0, "sources": 0, "keywords": 0, "analysed": 0, "leads": 0}
        )


class PostgresDatabase(Database):
    """asyncpg-backed storage for hosts with an ephemeral filesystem."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn, self._needs_ssl = split_ssl(dsn)
        self._min_size = min_size
        self._max_size = max_size
        self._pool = None  # type: ignore[assignment]

    async def connect(self) -> None:
        import asyncpg

        kwargs: dict = dict(
            min_size=self._min_size,
            max_size=self._max_size,
            # Managed databases (Neon) suspend the compute and drop idle sockets;
            # recycling them quickly keeps the pool from handing out dead ones.
            max_inactive_connection_lifetime=60,
        )
        if self._needs_ssl:
            kwargs["ssl"] = True
        self._pool = await asyncpg.create_pool(self.dsn, **kwargs)
        async with self._pool.acquire() as conn:
            await conn.execute(POSTGRES_SCHEMA)
            for statement in POSTGRES_MIGRATIONS:
                await conn.execute(statement)
        logger.info(
            "Postgres database ready (pool %s-%s)", self._min_size, self._max_size
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self):
        if self._pool is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._pool

    async def migrate(self) -> None:
        async with self.pool.acquire() as conn:
            for statement in POSTGRES_MIGRATIONS:
                await conn.execute(statement)

    async def add_chat(self, chat_id: int, title: str = "") -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO chats (chat_id, title, added_at) VALUES ($1, $2, $3)
                ON CONFLICT (chat_id) DO NOTHING
                RETURNING chat_id
                """,
                chat_id,
                title,
                _now(),
            )
            if row is None and title:
                await conn.execute(
                    "UPDATE chats SET title = $1 WHERE chat_id = $2 AND title = ''",
                    title,
                    chat_id,
                )
            return row is not None

    async def remove_chat(self, chat_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM chats WHERE chat_id = $1 RETURNING chat_id", chat_id
            )
            return row is not None

    async def list_chats(self) -> list[Chat]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chat_id, title, paused FROM chats ORDER BY added_at ASC"
            )
        return [
            Chat(chat_id=r["chat_id"], title=r["title"], paused=bool(r["paused"]))
            for r in rows
        ]

    async def is_monitored(self, chat_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM chats WHERE chat_id = $1 AND paused = 0 LIMIT 1", chat_id
            )
        return row is not None

    async def set_paused(self, chat_id: int, paused: bool) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE chats SET paused = $1 WHERE chat_id = $2 RETURNING chat_id",
                int(paused),
                chat_id,
            )
        return row is not None

    async def add_keyword(self, keyword: str) -> bool:
        keyword = keyword.strip().lower()
        if not keyword:
            return False
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO keywords (keyword, added_at) VALUES ($1, $2)
                ON CONFLICT (keyword) DO NOTHING
                RETURNING keyword
                """,
                keyword,
                _now(),
            )
        return row is not None

    async def remove_keyword(self, keyword: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM keywords WHERE keyword = $1 RETURNING keyword",
                keyword.strip().lower(),
            )
        return row is not None

    async def list_keywords(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT keyword FROM keywords ORDER BY added_at ASC")
        return [r["keyword"] for r in rows]

    async def match_keywords(self, text: str) -> list[str]:
        if not text:
            return []
        return match_keyword_list(await self.list_keywords(), text)

    async def is_duplicate(self, msg_hash: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM leads WHERE msg_hash = $1 LIMIT 1", msg_hash
            )
        return row is not None

    async def record_lead(
        self,
        msg_hash: str,
        chat_id: int,
        message_id: int,
        is_lead: bool,
        summary: str = "",
        intent: str = "",
        source: str = "",
        url: str = "",
        author: str = "",
        contact: str = "",
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO leads
                    (msg_hash, chat_id, message_id, is_lead, summary, intent,
                     source, url, author, contact, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (msg_hash) DO NOTHING
                RETURNING msg_hash
                """,
                msg_hash,
                chat_id,
                message_id,
                int(is_lead),
                summary,
                intent,
                source,
                url,
                author,
                contact,
                _now(),
            )
        return row is not None

    async def add_source(self, kind: str, target: str, title: str = "") -> bool:
        kind = (kind or "").strip().lower()
        target = (target or "").strip()
        if not kind or not target:
            return False
        source_id = f"{kind}:{target}"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sources (source_id, kind, target, title, added_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (source_id) DO NOTHING
                RETURNING source_id
                """,
                source_id,
                kind,
                target,
                title,
                _now(),
            )
            if row is None and title:
                await conn.execute(
                    "UPDATE sources SET title = $1 WHERE source_id = $2 AND title = ''",
                    title,
                    source_id,
                )
        return row is not None

    async def remove_source(self, source_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM sources WHERE source_id = $1 RETURNING source_id",
                source_id.strip(),
            )
        return row is not None

    async def list_sources(self, include_paused: bool = True) -> list[SourceRow]:
        sql = "SELECT source_id, kind, target, title, paused FROM sources"
        if not include_paused:
            sql += " WHERE paused = 0"
        sql += " ORDER BY added_at ASC"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [
            SourceRow(
                source_id=r["source_id"],
                kind=r["kind"],
                target=r["target"],
                title=r["title"],
                paused=bool(r["paused"]),
            )
            for r in rows
        ]

    async def get_source(self, source_id: str) -> SourceRow | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT source_id, kind, target, title, paused
                FROM sources WHERE source_id = $1
                """,
                source_id.strip(),
            )
        if row is None:
            return None
        return SourceRow(
            source_id=row["source_id"],
            kind=row["kind"],
            target=row["target"],
            title=row["title"],
            paused=bool(row["paused"]),
        )

    async def set_source_paused(self, source_id: str, paused: bool) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE sources SET paused = $1 WHERE source_id = $2 RETURNING source_id",
                int(paused),
                source_id.strip(),
            )
        return row is not None

    async def stats(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM chats)                   AS chats,
                    (SELECT COUNT(*) FROM sources)                 AS sources,
                    (SELECT COUNT(*) FROM keywords)                AS keywords,
                    (SELECT COUNT(*) FROM leads)                   AS analysed,
                    (SELECT COUNT(*) FROM leads WHERE is_lead = 1) AS leads
                """
            )
        return (
            dict(row)
            if row
            else {"chats": 0, "sources": 0, "keywords": 0, "analysed": 0, "leads": 0}
        )


def create_database(config) -> Database:
    """Return the storage backend selected by the configuration."""
    if config.use_postgres:
        return PostgresDatabase(config.database_url)
    return SQLiteDatabase(config.db_path)
