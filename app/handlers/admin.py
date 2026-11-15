"""Admin command handlers.

/start /addchat /addkeyword /list /remove /stats for manual control, plus
/pause /resume to withdraw a chat and /settings /export to read the config back.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..config import Config
from ..db import Database
from ..sources import SOURCE_KINDS, is_valid_channel, kind_help, normalise_channel

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _is_admin(message: Message, config: Config) -> bool:
    return message.from_user is not None and message.from_user.id == config.admin_id


async def _guard(message: Message, config: Config) -> bool:
    if _is_admin(message, config):
        return True
    await message.answer("⛔ Команда доступна только администратору.")
    logger.warning(
        "Unauthorized command from user_id=%s",
        message.from_user.id if message.from_user else "unknown",
    )
    return False


def _parse_chat_id(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


HELP_TEXT = (
    "🤖 <b>VA Lead Radar</b>\n\n"
    "Ищу заказы сам — на биржах и в публичных чатах/каналах — и присылаю "
    "карточки лидов с контактами.\n\n"
    "<b>Внешние источники (основной режим)</b>\n"
    "/sources — список источников и их состояние\n"
    "/addsource &lt;тип&gt; &lt;адрес&gt; — добавить источник\n"
    "/removesource &lt;тип&gt; &lt;адрес&gt; — удалить источник\n"
    "/pause_source, /resume_source — пауза источника\n"
    "/poll — опросить источники прямо сейчас\n\n"
    "<b>Чаты, куда меня добавили</b>\n"
    "/addchat &lt;chat_id&gt; — добавить чат в мониторинг\n"
    "/pause, /resume — пауза/снятие для текущего чата\n"
    "/remove chat &lt;chat_id&gt; — удалить чат\n\n"
    "<b>Ключевые слова</b>\n"
    "/addkeyword &lt;слово&gt; — добавить ключевое слово\n"
    "/remove keyword &lt;слово&gt; — удалить ключевое слово\n\n"
    "<b>Прочее</b>\n"
    "/list — показать чаты, источники и слова\n"
    "/settings — текущие настройки\n"
    "/export — выгрузить настройки файлом\n"
    "/stats — статистика\n\n"
    "ℹ️ На биржах и в публичных каналах бот ищет сам, добавлять его никуда не "
    "нужно. Обычные чаты (не каналы) Bot API читать не умеет — для них нужен "
    "userbot, см. README."
)


def _auto_mode_line(config: Config) -> str:
    if config.monitor_all_chats:
        return (
            "🟢 <b>Авторежим включён</b> — беру в мониторинг любой чат, "
            "куда меня добавили. Остановить конкретный чат: /pause в нём."
        )
    return (
        "⚪️ <b>Авторежим выключен</b> — мониторю только чаты, добавленные "
        "через /addchat. Включить: <code>MONITOR_ALL_CHATS=true</code>."
    )


@router.message(Command("start", "help"))
async def cmd_start(message: Message, config: Config) -> None:
    if not await _guard(message, config):
        return
    await message.answer(HELP_TEXT + "\n\n" + _auto_mode_line(config))


@router.message(Command("addchat"))
async def cmd_addchat(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    raw = (command.args or "").strip()
    chat_id = _parse_chat_id(raw) if raw else None
    title = ""
    if chat_id is None:
        await message.answer(
            "Использование: <code>/addchat -1001234567890</code>\n"
            "Подсказка: чтобы узнать id, перешлите сообщение из чата боту "
            "@userinfobot или посмотрите лог."
        )
        return

    added = await db.add_chat(chat_id, title)
    if added:
        await message.answer(f"✅ Чат <code>{chat_id}</code> добавлен в мониторинг.")
        logger.info("Chat %s added to monitoring", chat_id)
    else:
        await message.answer(f"ℹ️ Чат <code>{chat_id}</code> уже отслеживается.")


@router.message(Command("addkeyword"))
async def cmd_addkeyword(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    keyword = (command.args or "").strip()
    if not keyword:
        await message.answer(
            "Использование: <code>/addkeyword ищу разработчика</code>"
        )
        return

    if await db.add_keyword(keyword):
        await message.answer(f"✅ Ключевое слово добавлено: <b>{keyword}</b>")
        logger.info("Keyword added: %r", keyword)
    else:
        await message.answer(f"ℹ️ Ключевое слово уже есть: <b>{keyword}</b>")


@router.message(Command("list"))
async def cmd_list(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return

    chats = await db.list_chats()
    keywords = await db.list_keywords()
    sources = await db.list_sources()

    lines = ["📋 <b>Мониторинг</b>", "", _auto_mode_line(config), ""]
    lines.append("<b>Внешние источники:</b>")
    if sources:
        for row in sources:
            mark = "⏸" if row.paused else "•"
            lines.append(f"{mark} <code>{html.escape(row.source_id)}</code>")
    else:
        lines.append("• <i>пусто</i>")

    lines.append("")
    lines.append("<b>Чаты:</b>")
    if chats:
        for chat in chats:
            label = chat.title or "без названия"
            mark = "⏸" if chat.paused else "•"
            lines.append(f"{mark} <code>{chat.chat_id}</code> — {label}")
    else:
        lines.append("• <i>пусто</i>")

    lines.append("")
    lines.append("<b>Ключевые слова:</b>")
    if keywords:
        lines.extend(f"• {kw}" for kw in keywords)
    else:
        lines.append("• <i>пусто</i>")

    await message.answer("\n".join(lines))


@router.message(Command("remove"))
async def cmd_remove(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Использование:\n"
            "<code>/remove chat -1001234567890</code>\n"
            "<code>/remove keyword ищу разработчика</code>"
        )
        return

    parts = args.split(maxsplit=1)
    target = parts[0].lower()
    value = parts[1].strip() if len(parts) > 1 else ""

    if target in {"chat", "чат"}:
        chat_id = _parse_chat_id(value)
        if chat_id is None:
            await message.answer("Укажите числовой chat_id: <code>/remove chat -100...</code>")
            return
        if await db.remove_chat(chat_id):
            await message.answer(f"🗑 Чат <code>{chat_id}</code> удалён из мониторинга.")
            logger.info("Chat %s removed", chat_id)
        else:
            await message.answer(f"⚠️ Чат <code>{chat_id}</code> не найден.")
        return

    if target in {"keyword", "слово"}:
        if not value:
            await message.answer("Укажите слово: <code>/remove keyword ищу разработчика</code>")
            return
        if await db.remove_keyword(value):
            await message.answer(f"🗑 Ключевое слово удалено: <b>{value}</b>")
            logger.info("Keyword removed: %r", value)
        else:
            await message.answer(f"⚠️ Ключевое слово не найдено: <b>{value}</b>")
        return

    await message.answer(
        "Неизвестная цель. Используйте <code>chat</code> или <code>keyword</code>."
    )


@router.message(Command("pause", "resume"))
async def cmd_pause(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    paused = command.command.lower() == "pause"
    action = "поставлен на паузу" if paused else "снят с паузы"
    verb = "⏸" if paused else "▶️"

    raw = (command.args or "").strip()
    if raw:
        chat_id = _parse_chat_id(raw)
        if chat_id is None:
            await message.answer(
                f"Укажите числовой chat_id: <code>/{command.command} -100...</code>"
            )
            return
    else:
        # No argument: the chat the command was typed in must be a group.
        if message.chat.type not in {"group", "supergroup", "channel"}:
            await message.answer(
                f"В личке нечего ставить на паузу.\n"
                f"Напишите <code>/{command.command} -1001234567890</code> "
                "или отправьте команду прямо в нужном чате."
            )
            return
        chat_id = message.chat.id

    if not await db.set_paused(chat_id, paused):
        await message.answer(
            f"⚠️ Чат <code>{chat_id}</code> не найден в мониторинге.\n"
            + (
                "В авторежиме он появится сам после первого сообщения."
                if config.monitor_all_chats
                else "Сначала добавьте его: <code>/addchat " f"{chat_id}</code>"
            )
        )
        return

    await message.answer(f"{verb} Чат <code>{chat_id}</code> {action}.")
    logger.info("Chat %s paused=%s", chat_id, paused)


@router.message(Command("settings"))
async def cmd_settings(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return
    await message.answer(_settings_text(config, db))


@router.message(Command("export"))
async def cmd_export(message: Message, config: Config, db: Database) -> None:
    if not _is_admin(message, config):
        await message.answer("⛔ Команда доступна только администратору.")
        logger.warning(
            "Unauthorized /export from user_id=%s",
            message.from_user.id if message.from_user else "unknown",
        )
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    await message.answer_document(
        BufferedInputFile(
            (await _settings_json(config, db)).encode("utf-8"),
            filename=f"va-lead-radar-settings-{stamp}.json",
        ),
        caption="📦 Настройки VA Lead Radar (секреты не включены).",
    )


async def _settings_payload(config: Config, db: Database) -> dict:
    """Collect effective settings. Secrets are reported as set/not set only."""
    chats = await db.list_chats()
    sources = await db.list_sources()
    stats = await db.stats()
    return {
        "auto_mode": config.monitor_all_chats,
        "chats_total": len(chats),
        "chats_active": sum(1 for c in chats if not c.paused),
        "chats_paused": sum(1 for c in chats if c.paused),
        "keywords": await db.list_keywords(),
        "chats": [
            {"chat_id": c.chat_id, "title": c.title, "paused": c.paused} for c in chats
        ],
        "sources_total": len(sources),
        "sources_active": sum(1 for s in sources if not s.paused),
        "sources": [
            {
                "source_id": s.source_id,
                "kind": s.kind,
                "target": s.target,
                "title": s.title,
                "paused": s.paused,
            }
            for s in sources
        ],
        "polling": {
            "enabled": config.poll_sources,
            "interval_seconds": config.poll_interval,
            "seed_defaults": config.seed_default_sources,
        },
        "userbot_configured": bool(
            config.telegram_api_id and config.telegram_api_hash and config.telegram_session
        ),
        "llm": {
            "provider": config.llm_provider,
            "model": config.model,
            "fallback_models": list(config.llm_fallback_models),
            "base_url": config.openai_base_url,
        },
        "storage": "postgres" if config.use_postgres else "sqlite",
        "mode": "webhook" if config.use_webhook else "polling",
        "admin_configured": config.admin_id > 0,
        "max_analyze_chars": config.max_analyze_chars,
        "log_level": config.log_level,
        "stats": stats,
    }


async def _settings_json(config: Config, db: Database) -> str:
    payload = await _settings_payload(config, db)
    payload["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["env"] = {
        "MONITOR_ALL_CHATS": str(config.monitor_all_chats).lower(),
        "POLL_SOURCES": str(config.poll_sources).lower(),
        "POLL_INTERVAL": str(config.poll_interval),
        "SEED_DEFAULT_SOURCES": str(config.seed_default_sources).lower(),
        "LLM_PROVIDER": config.llm_provider,
        "LLM_MODEL": config.model,
        "LLM_FALLBACK_MODELS": ",".join(config.llm_fallback_models),
        "OPENAI_BASE_URL": config.openai_base_url,
        "MAX_ANALYZE_CHARS": str(config.max_analyze_chars),
        "LOG_LEVEL": config.log_level,
        "ADMIN_ID": str(config.admin_id),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _settings_text(config: Config, db: Database) -> str:
    data = await _settings_payload(config, db)
    llm = data["llm"]

    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        _auto_mode_line(config),
        "",
        f"<b>LLM:</b> {html.escape(llm['provider'])} / "
        f"<code>{html.escape(llm['model'])}</code>",
    ]
    if llm["fallback_models"]:
        fallbacks = ", ".join(html.escape(m) for m in llm["fallback_models"])
        lines.append(f"<b>Резервные модели:</b> <code>{fallbacks}</code>")
    if llm["provider"] == "openai":
        lines.append(f"<b>Шлюз:</b> <code>{html.escape(llm['base_url'])}</code>")

    lines += [
        f"<b>Хранилище:</b> {data['storage']}",
        f"<b>Режим:</b> {data['mode']}",
        f"<b>Админ задан:</b> {'да' if data['admin_configured'] else 'нет'}",
        f"<b>Лимит текста:</b> {data['max_analyze_chars']} символов",
        "",
        f"<b>Опрос источников:</b> "
        f"{'включён' if data['polling']['enabled'] else 'выключен'}, "
        f"каждые {data['polling']['interval_seconds']} с",
        f"<b>Userbot:</b> "
        f"{'настроен' if data['userbot_configured'] else 'не настроен'}",
        "",
        f"<b>Источники ({data['sources_active']} активных, "
        f"{data['sources_total'] - data['sources_active']} на паузе):</b>",
    ]

    if data["sources"]:
        for src in data["sources"]:
            mark = "⏸" if src["paused"] else "•"
            lines.append(f"{mark} <code>{html.escape(src['source_id'])}</code>")
    else:
        lines.append("• <i>пусто</i>")

    lines += [
        "",
        f"<b>Чаты ({data['chats_active']} активных, "
        f"{data['chats_paused']} на паузе):</b>",
    ]

    if data["chats"]:
        for chat in data["chats"]:
            mark = "⏸" if chat["paused"] else "•"
            title = html.escape(chat["title"] or "без названия")
            lines.append(f"{mark} <code>{chat['chat_id']}</code> — {title}")
    else:
        lines.append("• <i>пусто</i>")

    lines += ["", f"<b>Ключевые слова ({len(data['keywords'])}):</b>"]
    if data["keywords"]:
        lines.extend(f"• {html.escape(kw)}" for kw in data["keywords"])
    else:
        lines.append("• <i>пусто</i>")

    lines += [
        "",
        f"<b>Проанализировано:</b> {data['stats']['analysed']}, "
        f"<b>лидов:</b> {data['stats']['leads']}",
        "",
        "📦 Полная выгрузка: /export",
    ]
    return "\n".join(lines)


def _source_target(kind: str, raw: str) -> str:
    """Normalise a source target so ``@name`` and ``t.me/name`` match.

    Without this, the same channel added twice in different spellings would be
    polled twice and each post would arrive as two separate leads.
    """
    value = raw.strip()
    if kind == "tg_public":
        return normalise_channel(value)
    return value


def _source_id(kind: str, raw: str) -> str:
    return f"{kind}:{_source_target(kind, raw)}"


@router.message(Command("sources"))
async def cmd_sources(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return

    sources = await db.list_sources()
    lines = ["🌐 <b>Внешние источники</b>", ""]

    if sources:
        for row in sources:
            mark = "⏸" if row.paused else "•"
            lines.append(
                f"{mark} <code>{row.source_id}</code>\n"
                f"    {html.escape(row.display)}"
            )
    else:
        lines.append("• <i>пусто</i>")

    lines += [
        "",
        f"<b>Опрос:</b> {'включён' if config.poll_sources else 'выключен'}, "
        f"каждые {config.poll_interval} с",
        "",
        "<b>Как добавить</b>",
        kind_help(),
        "",
        "Пример: <code>/addsource tg_public freelance_orders</code>",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("addsource"))
async def cmd_addsource(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/addsource &lt;тип&gt; &lt;адрес&gt;</code>\n\n"
            + kind_help()
            + "\n\nПример: <code>/addsource rss https://www.freelancer.com/rss.xml</code>"
        )
        return

    kind, raw_target = parts[0].strip().lower(), parts[1].strip()
    if kind not in SOURCE_KINDS:
        await message.answer(
            f"⚠️ Неизвестный тип <code>{html.escape(kind)}</code>.\n\n" + kind_help()
        )
        return

    if kind == "userbot" and not config.telegram_session:
        await message.answer(
            "⚠️ Тип <code>userbot</code> требует ключей Telegram-аккаунта "
            "(<code>TELEGRAM_API_ID</code>, <code>TELEGRAM_API_HASH</code>, "
            "<code>TELEGRAM_SESSION</code>). Пока их нет, источник сохранится, "
            "но опрашиваться не будет. Инструкция — в README."
        )

    target = _source_target(kind, raw_target)
    if kind == "tg_public" and not is_valid_channel(target):
        await message.answer(
            f"⚠️ <code>{html.escape(raw_target)}</code> не похоже на публичный канал. "
            "Нужен @username канала (не приватная ссылка-приглашение)."
        )
        return

    if await db.add_source(kind, target):
        await message.answer(
            f"✅ Источник добавлен: <code>{html.escape(kind + ':' + target)}</code>\n"
            "Опрошу его в ближайшем цикле или сразу по /poll."
        )
        logger.info("Source added: %s:%s", kind, target)
    else:
        await message.answer(
            f"ℹ️ Источник уже есть: <code>{html.escape(kind + ':' + target)}</code>"
        )


@router.message(Command("removesource"))
async def cmd_removesource(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/removesource &lt;тип&gt; &lt;адрес&gt;</code>\n"
            "Например: <code>/removesource tg_public freelance_orders</code>"
        )
        return

    kind, raw_target = parts[0].strip().lower(), parts[1].strip()
    source_id = _source_id(kind, raw_target)
    if await db.remove_source(source_id):
        await message.answer(f"🗑 Источник удалён: <code>{html.escape(source_id)}</code>")
        logger.info("Source removed: %s", source_id)
    else:
        await message.answer(f"⚠️ Источник не найден: <code>{html.escape(source_id)}</code>")


@router.message(Command("pause_source", "resume_source"))
async def cmd_pause_source(
    message: Message, command: CommandObject, config: Config, db: Database
) -> None:
    if not await _guard(message, config):
        return

    paused = command.command.lower() == "pause_source"
    verb = "⏸" if paused else "▶️"
    action = "на паузе" if paused else "снова опрашивается"

    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            f"Использование: <code>/{command.command} &lt;тип&gt; &lt;адрес&gt;</code>\n"
            f"Например: <code>/{command.command} rss https://www.freelancer.com/rss.xml</code>"
        )
        return

    source_id = _source_id(parts[0].strip().lower(), parts[1].strip())
    if await db.set_source_paused(source_id, paused):
        await message.answer(f"{verb} Источник <code>{html.escape(source_id)}</code> {action}.")
        logger.info("Source %s paused=%s", source_id, paused)
    else:
        await message.answer(f"⚠️ Источник не найден: <code>{html.escape(source_id)}</code>")


@router.message(Command("poll"))
async def cmd_poll(message: Message, config: Config, poller) -> None:
    if not await _guard(message, config):
        return

    if poller is None:
        await message.answer("⚠️ Опрос источников недоступен в этом режиме.")
        return

    await message.answer("🔄 Опрошу источники, это может занять до минуты…")
    try:
        stats = await poller.run_once()
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
        logger.exception("Manual poll failed: %s", exc)
        await message.answer(f"⚠️ Опрос не удался: {html.escape(str(exc))}")
        return

    await message.answer(
        "✅ <b>Опрос завершён</b>\n"
        f"Источников: {stats['sources']}\n"
        f"Объявлений: {stats['items']}\n"
        f"Совпало по словам: {stats['matched']}\n"
        f"Лидов: {stats['leads']}\n"
        f"Отправлено карточек: {stats['sent']}"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, config: Config, db: Database) -> None:
    if not await _guard(message, config):
        return
    stats = await db.stats()
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Чатов: {stats['chats']}\n"
        f"Источников: {stats['sources']}\n"
        f"Ключевых слов: {stats['keywords']}\n"
        f"Проанализировано сообщений: {stats['analysed']}\n"
        f"Найдено лидов: {stats['leads']}"
    )
