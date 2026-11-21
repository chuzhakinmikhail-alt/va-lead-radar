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
    "ADMIN_IDS",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "DATABASE_URL",
    "DB_PATH",
    "LOG_PATH",
    "LOG_LEVEL",
    "MAX_ANALYZE_CHARS",
    "MAX_ITEM_AGE_DAYS",
    "TG_HISTORY_PAGES",
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


def test_tg_history_pages_default_and_override(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    assert load_config().tg_history_pages == 5

    clean_env.setenv("TG_HISTORY_PAGES", "8")
    assert load_config().tg_history_pages == 8

    # A nonsensical value must not disable the history walk entirely.
    clean_env.setenv("TG_HISTORY_PAGES", "0")
    assert load_config().tg_history_pages == 1


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


def test_admin_ids_parsed_and_merged(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    assert load_config().recipients == (42,)

    clean_env.setenv("ADMIN_IDS", "777, 888 ,777")
    cfg = load_config()
    # Duplicates collapse and ADMIN_ID stays first.
    assert cfg.recipients == (42, 777, 888)
    assert cfg.is_admin(888) and not cfg.is_admin(999)


def test_admin_ids_ignores_garbage(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("ADMIN_IDS", "abc,-5,0,777")
    assert load_config().recipients == (42, 777)


def test_no_admin_configured_means_no_recipients(clean_env):
    for k, v in REQUIRED.items():
        if k != "ADMIN_ID":
            clean_env.setenv(k, v)
    cfg = load_config()
    assert cfg.recipients == ()
    assert not cfg.is_admin(42)


def test_commands_are_open_by_default(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    cfg = load_config()
    assert cfg.open_commands is True
    assert cfg.can_manage(999) is True  # a stranger may manage
    assert cfg.is_admin(999) is False  # but is not an admin


def test_open_commands_can_be_closed(clean_env):
    for k, v in REQUIRED.items():
        clean_env.setenv(k, v)
    clean_env.setenv("OPEN_COMMANDS", "false")
    cfg = load_config()
    assert cfg.open_commands is False
    assert cfg.can_manage(999) is False
    assert cfg.can_manage(42) is True
