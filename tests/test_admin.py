"""Tests for the admin commands that read configuration back to the operator.

The settings/export helpers are exercised directly, and /pause through a
lightweight stand-in for aiogram's Message so the real handler code runs.
"""

import json

import pytest

from app.config import Config
from app.db import SQLiteDatabase
from app.handlers.admin import (
    _settings_json,
    _settings_text,
    cmd_pause,
)

SECRET = "sk-super-secret-key"


class FakeMessage:
    """Minimal Message: only the fields the handlers touch."""

    def __init__(self, chat_id: int = 555, chat_type: str = "private", user_id: int = 42):
        self.chat = type("Chat", (), {"id": chat_id, "type": chat_type})()
        self.from_user = type("User", (), {"id": user_id})()
        self.answers: list[str] = []
        self.documents: list[tuple[str, str]] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def answer_document(self, document, **kwargs):
        self.documents.append((document.filename, document.data.decode("utf-8")))


class FakeCommand:
    def __init__(self, command: str, args: str = "") -> None:
        self.command = command
        self.args = args


def make_config(admin_id: int = 42, **overrides) -> Config:
    base = dict(
        bot_token="1:abc",
        admin_id=admin_id,
        llm_api_key=SECRET,
        llm_provider="openai",
        llm_model="nvidia/nemotron-3.5-lightning:free",
        llm_fallback_models=("inclusionai/ling-3.2-flash:free",),
        openai_base_url="https://openrouter.ai/api/v1",
    )
    base.update(overrides)
    return Config(**base)


@pytest.mark.asyncio
async def test_settings_text_lists_state_and_hides_secret(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        await db.add_chat(-1002, "Paused Chat")
        await db.set_paused(-1002, True)
        await db.add_keyword("нужен бот")

        text = await _settings_text(make_config(), db)

        assert "Dev Chat" in text and "Paused Chat" in text
        assert "1 активных" in text and "1 на паузе" in text
        assert "нужен бот" in text
        assert "nvidia/nemotron-3.5-lightning:free" in text
        assert SECRET not in text


@pytest.mark.asyncio
async def test_settings_json_is_valid_and_secret_free(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        await db.add_keyword("нужен бот")

        payload = json.loads(await _settings_json(make_config(monitor_all_chats=True), db))

        assert payload["auto_mode"] is True
        assert payload["chats_active"] == 1
        assert payload["chats"] == [
            {"chat_id": -1001, "title": "Dev Chat", "paused": False}
        ]
        assert payload["keywords"] == ["нужен бот"]
        assert payload["env"]["MONITOR_ALL_CHATS"] == "true"
        assert SECRET not in json.dumps(payload)


@pytest.mark.asyncio
async def test_pause_command_uses_current_chat_in_group(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        message = FakeMessage(chat_id=-1001, chat_type="supergroup")

        await cmd_pause(message, FakeCommand("pause"), make_config(), db)

        assert await db.is_monitored(-1001) is False
        assert "поставлен на паузу" in message.answers[0]

        await cmd_pause(message, FakeCommand("resume"), make_config(), db)
        assert await db.is_monitored(-1001) is True


@pytest.mark.asyncio
async def test_pause_command_accepts_explicit_chat_id(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        message = FakeMessage(chat_type="private")  # typed in the admin DM

        await cmd_pause(message, FakeCommand("pause", "-1001"), make_config(), db)

        assert await db.is_monitored(-1001) is False


@pytest.mark.asyncio
async def test_pause_command_explains_private_chat_without_args(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage(chat_type="private")

        await cmd_pause(message, FakeCommand("pause"), make_config(), db)

        assert "нечего ставить на паузу" in message.answers[0]


@pytest.mark.asyncio
async def test_pause_command_reports_unknown_chat(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        message = FakeMessage(chat_type="private")

        await cmd_pause(message, FakeCommand("pause", "-9999"), make_config(), db)

        assert "не найден" in message.answers[0]


@pytest.mark.asyncio
async def test_pause_command_rejects_non_admin(tmp_path):
    async with SQLiteDatabase(tmp_path / "t.db") as db:
        await db.add_chat(-1001, "Dev Chat")
        message = FakeMessage(chat_id=-1001, chat_type="supergroup", user_id=999)

        await cmd_pause(message, FakeCommand("pause"), make_config(), db)

        assert "только администратору" in message.answers[0]
        assert await db.is_monitored(-1001) is True
