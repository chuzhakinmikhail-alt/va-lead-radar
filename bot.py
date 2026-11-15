"""Long-polling entry point.

Use this locally, on a VPS or in Docker — anywhere a process can stay alive and
no public URL is needed. On a free host (Render/Railway) the filesystem is wiped
and the service sleeps, so run ``main.py`` (webhook mode) instead.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import ConfigError, load_config
from app.logger import setup_logging
from app.runtime import build_runtime, set_commands

logger = logging.getLogger(__name__)


async def run() -> None:
    config = load_config()
    setup_logging(config.log_path, config.log_level)
    logger.info(
        "Starting VA Lead Radar (polling, provider=%s, model=%s, storage=%s)",
        config.llm_provider,
        config.model,
        "postgres" if config.use_postgres else "sqlite",
    )

    runtime = await build_runtime(config)
    await set_commands(runtime.bot)
    me = await runtime.bot.get_me()
    logger.info("Bot authorised as @%s (id=%s)", me.username, me.id)

    try:
        # Long polling replaces any webhook for this token; that is intended here.
        await runtime.bot.delete_webhook(drop_pending_updates=False)
        await runtime.dispatcher.start_polling(
            runtime.bot, allowed_updates=runtime.dispatcher.resolve_used_update_types()
        )
    finally:
        logger.info("Shutting down…")
        await runtime.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"⚠️ НЕВОЗМОЖНО запустить без конфигурации: {exc}")
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - top-level safety net
        logging.getLogger(__name__).exception("Fatal error: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
