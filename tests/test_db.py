"""Storage tests: the shared behaviour runs against both backends.

The SQLite backend always runs. The Postgres backend is skipped unless
``TEST_DATABASE_URL`` points at a reachable server, so the suite stays green on a
laptop with no Postgres.
"""

import os

import pytest

from app.db import SQLiteDatabase, split_ssl


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteDatabase(tmp_path / "t.db")

    dsn = os.getenv("TEST_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres backend")

    from app.db import PostgresDatabase

    return PostgresDatabase(dsn)


async def _reset(db) -> None:
    """Start each case from an empty database."""
    if isinstance(db, SQLiteDatabase):
        return
    async with db.pool.acquire() as conn:
        await conn.execute("TRUNCATE chats, keywords, leads")


@pytest.mark.asyncio
async def test_chat_lifecycle(backend):
    async with backend as db:
        await _reset(db)
        assert await db.add_chat(-1001, "Group A") is True
        assert await db.add_chat(-1001) is False  # duplicate
        assert await db.is_monitored(-1001) is True
        chats = await db.list_chats()
        assert len(chats) == 1 and chats[0].chat_id == -1001
        assert await db.remove_chat(-1001) is True
        assert await db.is_monitored(-1001) is False


@pytest.mark.asyncio
async def test_keywords_seed_and_match(backend):
    async with backend as db:
        await _reset(db)
        assert await db.list_keywords() == []
        assert await db.seed_keywords(("нужен сайт", "нужен бот")) == 2
        assert await db.seed_keywords(("нужен сайт", "нужен бот")) == 0
        matched = await db.match_keywords("Привет, НУЖЕН САЙТ для компании")
        assert matched == ["нужен сайт"]
        assert await db.match_keywords("просто болтаем") == []


@pytest.mark.asyncio
async def test_anti_duplicate(backend):
    async with backend as db:
        await _reset(db)
        from app.db import message_hash

        h = message_hash(-1001, 42)
        assert await db.is_duplicate(h) is False
        assert await db.record_lead(h, -1001, 42, True, "s", "i") is True
        assert await db.record_lead(h, -1001, 42, True, "s", "i") is False
        assert await db.is_duplicate(h) is True
        stats = await db.stats()
        assert stats["leads"] == 1 and stats["analysed"] == 1


@pytest.mark.asyncio
async def test_pause_hides_chat_from_monitoring(backend):
    async with backend as db:
        await _reset(db)
        await db.add_chat(-1001, "Group A")
        assert await db.is_monitored(-1001) is True

        assert await db.set_paused(-1001, True) is True
        assert await db.is_monitored(-1001) is False
        # The row survives, so auto mode cannot re-adopt a paused chat.
        chats = await db.list_chats()
        assert len(chats) == 1 and chats[0].paused is True

        assert await db.set_paused(-1001, False) is True
        assert await db.is_monitored(-1001) is True
        assert (await db.list_chats())[0].paused is False


@pytest.mark.asyncio
async def test_set_paused_unknown_chat_reports_false(backend):
    async with backend as db:
        await _reset(db)
        assert await db.set_paused(-9999, True) is False


@pytest.mark.asyncio
async def test_migrate_is_idempotent(backend):
    async with backend as db:
        # connect() already migrated; running it again must not raise.
        await db.migrate()
        await db.migrate()
        assert await db.add_chat(-1001, "Group A") is True


def test_message_hash_is_stable_and_unique():
    from app.db import message_hash

    assert message_hash(1, 2) == message_hash(1, 2)
    assert message_hash(1, 2) != message_hash(2, 1)


def test_split_ssl_strips_libpq_only_params():
    dsn = (
        "postgresql://u:p@host/db"
        "?sslmode=require&channel_binding=require&target_session_attrs=read-write"
    )
    clean, needs_ssl = split_ssl(dsn)
    assert needs_ssl is True
    assert "channel_binding" not in clean
    assert "sslmode" not in clean
    assert "target_session_attrs" not in clean


def test_split_ssl_plain_dsn_untouched():
    dsn = "postgresql://u:p@host/db"
    clean, needs_ssl = split_ssl(dsn)
    assert clean == dsn and needs_ssl is False
