"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# Keywords applied on first run when the database has none yet.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "ищу разработчика",
    "нужен сайт",
    "нужен бот",
    "закажу дизайн",
)

# Sensible default model per provider.
DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-1.5-flash",
    "openai": "gpt-4o-mini",
}

SUPPORTED_PROVIDERS = tuple(DEFAULT_MODELS)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    llm_api_key: str
    llm_provider: str = "gemini"
    llm_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "va_lead_radar.db")
    log_path: Path = field(default_factory=lambda: BASE_DIR / "logs" / "bot.log")
    log_level: str = "INFO"
    max_analyze_chars: int = 4000
    default_keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    # Postgres DSN (Neon / Render Postgres). Empty keeps the local SQLite file,
    # which is only safe on hosts with a persistent disk.
    database_url: str = ""
    # Empty WEBHOOK_BASE_URL runs long polling (local/VPS). A public https URL
    # switches the bot to webhook mode, which free hosts require.
    webhook_base_url: str = ""
    webhook_secret: str = ""
    webhook_path: str = "/telegram/webhook"
    port: int = 8080

    @property
    def model(self) -> str:
        """Model name, falling back to the provider default."""
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")

    @property
    def use_webhook(self) -> bool:
        """True when a public base URL was configured."""
        return bool(self.webhook_base_url)

    @property
    def use_postgres(self) -> bool:
        return bool(self.database_url)


def _resolve_path(raw: str | None, default: Path) -> Path:
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def load_config() -> Config:
    """Build a :class:`Config` from the environment, validating required keys.

    Only ``BOT_TOKEN`` and ``LLM_API_KEY`` are mandatory. ``ADMIN_ID`` is optional
    so the service can boot before the operator fills it in: without it the bot
    still monitors and records leads, but cannot deliver cards (a warning is
    logged at startup).
    """
    missing: list[str] = []

    bot_token = _env("BOT_TOKEN")
    if not bot_token:
        missing.append("BOT_TOKEN")

    llm_api_key = _env("LLM_API_KEY")
    if not llm_api_key:
        missing.append("LLM_API_KEY")

    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    admin_id = _env_int("ADMIN_ID", 0)

    provider = (_env("LLM_PROVIDER", "gemini") or "gemini").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"LLM_PROVIDER must be one of {SUPPORTED_PROVIDERS}, got: {provider!r}"
        )

    database_url = _env("DATABASE_URL", "") or ""
    # Render's filesystem is wiped on every deploy and restart, so the SQLite
    # fallback would silently lose chats, keywords and lead hashes. Refuse to
    # start in that mode rather than look healthy while dropping data.
    if not database_url and _env("RENDER_EXTERNAL_URL"):
        raise ConfigError(
            "DATABASE_URL is not set. Render's filesystem is ephemeral, so leads "
            "stored in SQLite would be lost on the next restart. Add DATABASE_URL "
            "with your Neon (or Render Postgres) connection string, including "
            "?sslmode=require."
        )

    base_url = (_env("WEBHOOK_BASE_URL", "") or "").rstrip("/")
    if not base_url:
        # Render injects the public service URL; auto-detect so it cannot be wrong.
        base_url = (_env("RENDER_EXTERNAL_URL", "") or "").rstrip("/")

    return Config(
        bot_token=str(bot_token),
        admin_id=admin_id,
        llm_api_key=str(llm_api_key),
        llm_provider=provider,
        llm_model=_env("LLM_MODEL", "") or "",
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1")
        or "https://api.openai.com/v1",
        db_path=_resolve_path(_env("DB_PATH"), BASE_DIR / "data" / "va_lead_radar.db"),
        log_path=_resolve_path(_env("LOG_PATH"), BASE_DIR / "logs" / "bot.log"),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        max_analyze_chars=max(200, _env_int("MAX_ANALYZE_CHARS", 4000)),
        database_url=database_url,
        webhook_base_url=base_url,
        webhook_secret=_env("WEBHOOK_SECRET", "") or "",
        webhook_path=_env("WEBHOOK_PATH", "/telegram/webhook") or "/telegram/webhook",
        port=_env_int("PORT", 8080),
    )
