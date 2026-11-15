"""Config tests: required keys, Postgres/webhook detection, Render guard."""

import pytest

from app.config import ConfigError, load_config

REQUIRED = {
    "BOT_TOKEN": "1:abc",
    "LLM_API_KEY": "k",
    "ADMIN_ID": "42",
}

# Every variable load_config reads; cleared before each case for isolation.
_TOUCHED = [
    "BOT_TOKEN",
    "LLM_API_KEY",
    "ADMIN_ID",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "DATABASE_URL",
    "DB_PATH",
    "LOG_PATH",
    "LOG_LEVEL",
    "MAX_ANALYZE_CHARS",
    "WEBHOOK_BASE_URL",
    "WEBHOOK_SECRET",
    "WEBHOOK_PATH",
    "PORT",
    "RENDER_EXTERNAL_URL",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in _TOUCHED:
        monkeypatch.delenv(name, raising=False)
    # Point .env loading away from any real file in the repo.
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_missing_required_keys_fail(clean_env):
    clean_env.setenv("BOT_TOKEN", "1:abc")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "LLM_API_KEY" in str(exc.value)


def test_admin_id_is_optional(clean_env):
    for k, v in REQUIRED.items():
        if k != "ADMIN_ID":
            clean_env.setenv(k, v)
    cfg = load_config()
    assert cfg.admin_id == 0


def test_full_config(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    cfg = load_config()
    assert cfg.admin_id == 42
    assert cfg.llm_provider == "gemini"
    assert cfg.model == "gemini-1.5-flash"
    assert cfg.use_postgres is False
    assert cfg.use_webhook is False


def test_database_url_switches_to_postgres(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@h/db?sslmode=require")
    assert load_config().use_postgres is True


def test_render_external_url_enables_webhook(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@h/db?sslmode=require")
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://va-lead-radar.onrender.com")
    cfg = load_config()
    assert cfg.use_webhook is True
    assert cfg.webhook_base_url == "https://va-lead-radar.onrender.com"


def test_render_without_database_url_refuses_to_start(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://va-lead-radar.onrender.com")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "DATABASE_URL" in str(exc.value)


def test_invalid_provider_rejected(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ConfigError):
        load_config()
