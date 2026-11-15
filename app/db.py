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
keywords : trigger keywords (case-insensitive, stored lowercased).
leads    : every analysed message; the primary key doubles as anti-duplicate
           protection so one message is never forwarded twice.
"""

from __future__ import annotations

import hashlib
import logging
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

CREATE TABLE IF NOT EXISTS keywords (
    keyword    TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    msg_hash   TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    is_lead    INTEGER NOT NULL,
    summary    TEXT    NOT NULL DEFAULT '',
    intent     TEXT    NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS keywords (
    keyword    TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    msg_hash   TEXT PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    is_lead    INTEGER NOT NULL,
    summary    TEXT   NOT NULL DEFAULT '',
    intent     TEXT   NOT NULL DEFAULT '',
    created_at TEXT   NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads (created_at);
"""

# Columns added after the first release, applied at startup. Postgres supports
# IF NOT EXISTS; SQLite does not, so its backend inspects the table instead.
POSTGRES_MIGRATIONS = (
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS paused INTEGER NOT NULL DEFAULT 0",
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def message_hash(chat_id: int, message_id: int) -> str:
    """Stable hash identifying a single message inside a chat."""
    raw = f"{chat_id}:{message_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    ) -> bool: ...

    @abstractmethod
    async def stats(self) -> dict[str, int]: ...

    async def seed_keywords(self, keywords: tuple[str, ...] | list[str]) -> int:
        """Insert default keywords, but only when the table is empty."""
        if await self.list_keywords():
            return 0
        added = 0
        for keyword in keywords:
            if await self.add_keyword(keyword):
                added += 1
        if added:
            logger.info("Seeded %d default keyword(s)", added)
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
        cur = await self.conn.execute("PRAGMA table_info(chats)")
        columns = {r["name"] for r in await cur.fetchall()}
        if "paused" not in columns:
            await self.conn.execute(
                "ALTER TABLE chats ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()
            logger.info("Migrated chats table: added paused column")

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
        haystack = text.lower()
        return [kw for kw in await self.list_keywords() if kw in haystack]

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
    ) -> bool:
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO leads
                (msg_hash, chat_id, message_id, is_lead, summary, intent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_hash, chat_id, message_id, int(is_lead), summary, intent, _now()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def stats(self) -> dict[str, int]:
        cur = await self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chats)                   AS chats,
                (SELECT COUNT(*) FROM keywords)                AS keywords,
                (SELECT COUNT(*) FROM leads)                   AS analysed,
                (SELECT COUNT(*) FROM leads WHERE is_lead = 1) AS leads
            """
        )
        row = await cur.fetchone()
        return (
            dict(row) if row else {"chats": 0, "keywords": 0, "analysed": 0, "leads": 0}
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
        haystack = text.lower()
        return [kw for kw in await self.list_keywords() if kw in haystack]

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
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO leads
                    (msg_hash, chat_id, message_id, is_lead, summary, intent, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (msg_hash) DO NOTHING
                RETURNING msg_hash
                """,
                msg_hash,
                chat_id,
                message_id,
                int(is_lead),
                summary,
                intent,
                _now(),
            )
        return row is not None

    async def stats(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM chats)                   AS chats,
                    (SELECT COUNT(*) FROM keywords)                AS keywords,
                    (SELECT COUNT(*) FROM leads)                   AS analysed,
                    (SELECT COUNT(*) FROM leads WHERE is_lead = 1) AS leads
                """
            )
        return (
            dict(row) if row else {"chats": 0, "keywords": 0, "analysed": 0, "leads": 0}
        )


def create_database(config) -> Database:
    """Return the storage backend selected by the configuration."""
    if config.use_postgres:
        return PostgresDatabase(config.database_url)
    return SQLiteDatabase(config.db_path)
