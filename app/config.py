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

# Extra keywords seeded for external sources. Live boards rarely repeat the
# exact chat phrasings above, so these broader signals widen the net. Precision
# comes from the LLM stage, which drops ads and chatter, so a broad keyword here
# costs one classification call rather than a false lead.
DEFAULT_SOURCE_KEYWORDS: tuple[str, ...] = (
    "нужен разработчик",
    "ищу специалиста",
    "нужен специалист",
    "ищу исполнителя",
    "ищу программиста",
    "нужен программист",
    "ищу дизайнера",
    "нужен дизайнер",
    "ищу верстальщика",
    "нужен верстальщик",
    "требуется разработчик",
    "ищу фрилансера",
    "нужен фрилансер",
    "ищу подрядчика",
    "нужен подрядчик",
    "ищу человека",
    "нужна помощь с",
    "кто может сделать",
    "разработка на заказ",
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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    """Read a comma- or newline-separated list from the environment."""
    raw = _env(name, "") or ""
    return tuple(item.strip() for item in raw.replace("\n", ",").split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    llm_api_key: str
    llm_provider: str = "gemini"
    llm_model: str = ""
    # Tried in order when the primary model fails (rate limits, outages).
    # Free OpenRouter models return 429 often, so a chain keeps the bot useful.
    llm_fallback_models: tuple[str, ...] = ()
    openai_base_url: str = "https://api.openai.com/v1"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "va_lead_radar.db")
    log_path: Path = field(default_factory=lambda: BASE_DIR / "logs" / "bot.log")
    log_level: str = "INFO"
    max_analyze_chars: int = 4000
    default_keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    # Auto-monitor every chat the bot is added to, instead of requiring
    # /addchat. Chats are registered on their first message; /pause withdraws one.
    monitor_all_chats: bool = False
    # Poll external sources (freelance feeds, public Telegram channels) so the
    # bot finds orders on its own, without being added to any chat.
    poll_sources: bool = True
    poll_interval: int = 900
    # Drop postings older than this. A source whose feed has stopped still
    # serves its last post forever, and that stale order would otherwise be
    # reported as a fresh lead on the first poll. 0 disables the check.
    max_item_age_days: int = 30
    # How many ~20-post preview pages of a public channel to read per poll. More
    # pages survive a host that sleeps between cycles; the anti-duplicate hash
    # makes the overlap free. 5 pages is ~100 posts, roughly a day on a busy
    # channel, so a single wake after a long sleep still recovers the backlog.
    tg_history_pages: int = 5
    seed_default_sources: bool = True
    # Optional userbot (MTProto) credentials. Only needed to read ordinary
    # chats or run global search; public channels work without them.
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session: str = ""
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
    def models(self) -> tuple[str, ...]:
        """Primary model followed by the configured fallbacks, deduplicated."""
        ordered = (self.model, *self.llm_fallback_models)
        seen: list[str] = []
        for name in ordered:
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)

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
        llm_fallback_models=_env_list("LLM_FALLBACK_MODELS"),
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1")
        or "https://api.openai.com/v1",
        db_path=_resolve_path(_env("DB_PATH"), BASE_DIR / "data" / "va_lead_radar.db"),
        log_path=_resolve_path(_env("LOG_PATH"), BASE_DIR / "logs" / "bot.log"),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        max_analyze_chars=max(200, _env_int("MAX_ANALYZE_CHARS", 4000)),
        monitor_all_chats=_env_bool("MONITOR_ALL_CHATS", False),
        poll_sources=_env_bool("POLL_SOURCES", True),
        poll_interval=max(60, _env_int("POLL_INTERVAL", 900)),
        max_item_age_days=max(0, _env_int("MAX_ITEM_AGE_DAYS", 30)),
        tg_history_pages=max(1, _env_int("TG_HISTORY_PAGES", 5)),
        seed_default_sources=_env_bool("SEED_DEFAULT_SOURCES", True),
        telegram_api_id=_env_int("TELEGRAM_API_ID", 0),
        telegram_api_hash=_env("TELEGRAM_API_HASH", "") or "",
        telegram_session=_env("TELEGRAM_SESSION", "") or "",
        database_url=database_url,
        webhook_base_url=base_url,
        webhook_secret=_env("WEBHOOK_SECRET", "") or "",
        webhook_path=_env("WEBHOOK_PATH", "/telegram/webhook") or "/telegram/webhook",
        port=_env_int("PORT", 8080),
    )
