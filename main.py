"""Webhook entry point for free hosts (Render, Railway, Fly, Heroku-style).

Long polling needs a host that keeps a process alive forever, which free plans do
not offer: they give you a *web service* and sleep it after ~15 idle minutes. This
module solves both problems:

* It serves the bot over a real HTTP port, so the host accepts it as a web service.
* It exposes ``GET /health``, which a free external monitor (cron-job.org,
  UptimeRobot) pings every 5 minutes to keep the instance awake.

Run locally:   python main.py   (with WEBHOOK_BASE_URL set to a public https URL)
Run on Render: start command ``python main.py``, health check path ``/health``
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import AsyncIterator, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.config import Config, ConfigError, load_config
from app.logger import setup_logging
from app.runtime import Runtime, build_runtime, set_commands

logger = logging.getLogger(__name__)

SERVICE_NAME = "va-lead-radar"


def build_version() -> str:
    """Commit id Render injects, so it is obvious whether a fix is deployed."""
    return (
        os.getenv("RENDER_GIT_COMMIT", "").strip()
        or os.getenv("GIT_COMMIT", "").strip()
        or "dev"
    )


def _webhook_url(config: Config) -> str:
    """Full public URL Telegram should POST updates to."""
    return f"{config.webhook_base_url}{config.webhook_path}"


def _telegram_secret(config: Config) -> Optional[str]:
    """Derive a secret token Telegram accepts from the configured value.

    Telegram allows only A-Z, a-z, 0-9, '_' and '-' in ``secret_token``. Render's
    ``generateValue: true`` produces base64, which contains '+' and '/', so
    Telegram answers "secret token contains illegal characters" and the webhook is
    never registered — the service looks healthy, /health answers ok, and every
    update is lost. Hashing yields a valid token that is still unguessable and
    stable across restarts.

    Returns:
        A hex digest, or None when no secret is configured (local runs).
    """
    raw = (config.webhook_secret or "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _set_webhook(bot: Bot, config: Config) -> None:
    """Register the webhook with Telegram, replacing any previous polling setup."""
    await bot.set_webhook(
        url=_webhook_url(config),
        secret_token=_telegram_secret(config),
        # Must stay False. Telegram keeps undelivered updates for 24h, so a message
        # arriving while a free host is asleep is queued and redelivered on the next
        # start. Dropping pending updates would throw exactly those messages away.
        drop_pending_updates=False,
        allowed_updates=["message", "edited_message", "channel_post"],
    )
    logger.info("Webhook registered: %s", _webhook_url(config))


async def _register_webhook(
    bot: Bot, config: Config, attempts: int = 5, delay_seconds: float = 3.0
) -> bool:
    """Register the webhook, retrying only transient failures.

    A single network blip used to abort startup, which a host reports as a failed
    deploy. Retrying keeps a deploy from failing for a reason that fixes itself.

    Raises:
        TelegramUnauthorizedError: when the token is rejected. That never fixes
            itself, so the deploy must fail loudly instead of serving a bot that
            can never receive an update.
    """
    for attempt in range(1, attempts + 1):
        try:
            await _set_webhook(bot, config)
            if attempt > 1:
                logger.info("Webhook registered on attempt %s", attempt)
            return True
        except TelegramUnauthorizedError:
            logger.error("Telegram rejected BOT_TOKEN; check the value on the host.")
            raise
        except Exception as exc:  # noqa: BLE001 - treat everything else as transient
            if attempt == attempts:
                logger.error(
                    "Webhook registration failed after %s attempts: %s", attempts, exc
                )
                return False
            logger.warning(
                "Webhook registration attempt %s/%s failed: %s. Retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay_seconds,
            )
            await asyncio.sleep(delay_seconds)
    return False


async def _retry_webhook_forever(bot: Bot, config: Config) -> None:
    """Keep trying to register the webhook so the service heals on its own."""
    while True:
        await asyncio.sleep(60)
        try:
            await _set_webhook(bot, config)
            logger.info("Webhook registered by the background retry.")
            return
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.warning("Background webhook retry failed: %s", exc)


def create_app(runtime: Runtime) -> web.Application:
    """Build the aiohttp application that serves updates and /health."""
    config = runtime.config

    async def health(_request: web.Request) -> web.Response:
        """Liveness probe for the host and the keep-alive monitor."""
        stats = {"chats": 0, "keywords": 0, "analysed": 0, "leads": 0}
        try:
            stats = await runtime.db.stats()
        except Exception as exc:  # noqa: BLE001 - health must not fail on stats
            logger.warning("Health stats unavailable: %s", exc)
        return web.json_response(
            {
                "status": "ok",
                "bot": SERVICE_NAME,
                "mode": "webhook",
                "version": build_version(),
                "admin_configured": config.admin_id > 0,
                "monitor_all_chats": config.monitor_all_chats,
                "storage": "postgres" if config.use_postgres else "sqlite",
                "stats": stats,
            }
        )

    # aiohttp's cleanup_ctx wants an async *generator* function (it calls
    # cb(app).__aiter__()), so this must not be wrapped in @asynccontextmanager.
    async def lifespan(_app: web.Application) -> AsyncIterator[None]:
        retry_task: Optional[asyncio.Task[None]] = None
        try:
            await set_commands(runtime.bot)
            # Serving /health matters even when registration fails: the host keeps
            # the deploy alive and the background task registers the webhook once
            # Telegram is reachable again.
            registered = await _register_webhook(runtime.bot, config)
            if not registered:
                retry_task = asyncio.create_task(
                    _retry_webhook_forever(runtime.bot, config)
                )
            yield
        finally:
            if retry_task is not None:
                retry_task.cancel()
                try:
                    await retry_task
                except asyncio.CancelledError:
                    pass
            # The webhook is deliberately NOT deleted here. It belongs to the bot
            # token, not to this process, and Telegram keeps it until something
            # replaces it. A host that rolls out a new instance before stopping the
            # old one would otherwise have the webhook erased by the outgoing
            # process, leaving a service that is "Live", answers /health and
            # receives nothing. To switch back to long polling, call deleteWebhook
            # for the token explicitly.
            await runtime.shutdown()
            logger.info("Webhook server stopped.")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    handler = SimpleRequestHandler(
        dispatcher=runtime.dispatcher,
        bot=runtime.bot,
        secret_token=_telegram_secret(config),
    )
    handler.register(app, path=config.webhook_path)
    setup_application(app, runtime.dispatcher, bot=runtime.bot)
    app.cleanup_ctx.append(lifespan)
    return app


async def main() -> None:
    """Start the webhook web server on the port the host assigned."""
    config = load_config()
    setup_logging(config.log_path, config.log_level)
    if not config.use_webhook:
        raise ConfigError(
            "WEBHOOK_BASE_URL is not set. Free hosts need webhook mode; set it to "
            "your public https URL (e.g. https://my-bot.onrender.com)."
        )

    logger.info(
        "Starting VA Lead Radar (webhook, provider=%s, model=%s, storage=%s)",
        config.llm_provider,
        config.model,
        "postgres" if config.use_postgres else "sqlite",
    )
    runtime = await build_runtime(config)
    app = create_app(runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.port)
    await site.start()
    logger.info("Listening on 0.0.0.0:%s (health check: /health)", config.port)
    try:
        await asyncio.Event().wait()  # run until the process is stopped
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"⚠️ НЕВОЗМОЖНО запустить без конфигурации: {exc}")
        raise SystemExit(2) from exc
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested by the operator")
