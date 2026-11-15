"""Webhook entry point tests: /health, secret derivation and webhook URL.

No network: the Telegram calls are stubbed, so the aiohttp app is exercised for
real (routes, JSON shape) without touching api.telegram.org.
"""

from aiohttp.test_utils import TestClient, TestServer
import pytest
from aiogram import Dispatcher

from app.config import Config
from app.db import SQLiteDatabase
from main import _telegram_secret, _webhook_url, build_version, create_app


class FakeSession:
    """Stand-in for aiogram's session; SimpleRequestHandler closes it on shutdown."""

    async def close(self) -> None:
        return None


class FakeBot:
    """Minimal Bot stand-in.

    ``set_webhook`` succeeds so the app's lifespan does not fall into its retry /
    background-retry path, which would otherwise sleep and hang the test.
    """

    def __init__(self) -> None:
        self.session = FakeSession()
        self.webhooks: list[dict] = []

    async def set_webhook(self, **kwargs) -> None:
        self.webhooks.append(kwargs)

    async def set_my_commands(self, commands) -> None:
        return None


class FakeRuntime:
    """Runtime with a real Dispatcher, so create_app wires up normally."""

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.bot = FakeBot()
        self.dispatcher = Dispatcher()

    async def shutdown(self) -> None:  # pragma: no cover - not reached in tests
        pass


def make_config(tmp_path) -> Config:
    return Config(
        bot_token="1:abc",
        admin_id=42,
        llm_api_key="k",
        llm_provider="openai",
        db_path=tmp_path / "t.db",
        webhook_base_url="https://va-lead-radar.onrender.com",
        webhook_secret="base64/secret+with=chars",
        webhook_path="/telegram/webhook",
    )


def test_telegram_secret_is_telegram_legal():
    config = Config(
        bot_token="1:abc",
        admin_id=1,
        llm_api_key="k",
        webhook_secret="aB+c/D==",
    )
    secret = _telegram_secret(config)
    assert secret is not None
    assert set(secret) <= set("0123456789abcdef")
    # Stable across calls, and empty secret yields None (local runs).
    assert secret == _telegram_secret(config)
    assert _telegram_secret(Config(bot_token="1:a", admin_id=1, llm_api_key="k")) is None


def test_webhook_url_joins_base_and_path():
    config = Config(
        bot_token="1:a",
        admin_id=1,
        llm_api_key="k",
        webhook_base_url="https://x.onrender.com",
        webhook_path="/telegram/webhook",
    )
    assert _webhook_url(config) == "https://x.onrender.com/telegram/webhook"


def test_build_version_default(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert build_version() == "dev"
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    assert build_version() == "abc123"


@pytest.mark.asyncio
async def test_health_endpoint_reports_ok(tmp_path):
    db = SQLiteDatabase(tmp_path / "t.db")
    await db.connect()
    await db.add_chat(-1001, "Dev Chat")
    await db.add_keyword("нужен бот")
    config = make_config(tmp_path)
    runtime = FakeRuntime(config, db)
    app = create_app(runtime)

    async with TestClient(TestServer(app)) as client:
        for path in ("/", "/health"):
            resp = await client.get(path)
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["mode"] == "webhook"
            assert body["storage"] == "sqlite"
            assert body["admin_configured"] is True
            assert body["stats"]["chats"] == 1
            assert body["stats"]["keywords"] == 1

    await db.close()
