# AGENTS.md — VA Lead Radar

Контекст для будущих сессий. Читается автоматически.

## Что это

Telegram-бот: следит за чатами, ищет ключевые слова, через LLM решает, лид это
или спам, и присылает админу карточку. Python 3.11, aiogram 3.x, SQLite или
Postgres, Gemini/OpenAI.

## Команды

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # 28 тестов, 3 скипаются без TEST_DATABASE_URL
python bot.py               # long polling (локально)
python main.py              # webhook + /health (хостинг)
```

## Структура

- `bot.py` — long polling. `main.py` — webhook + `/health`.
- `app/runtime.py` — общая сборка (бот, диспетчер, база, анализатор) для обоих
  входов. Меняя состав роутеров или зависимости, правьте только здесь.
- `app/config.py` — `load_config()`, единственное место чтения env.
- `app/db.py` — `Database` (ABC) + `SQLiteDatabase` + `PostgresDatabase`.
  Новый метод добавляется в ABC и в оба класса.
- `app/analyzer.py` — LLM, `LeadVerdict(is_lead, summary, intent)`.
- `app/handlers/admin.py` — команды. `app/handlers/monitor.py` — новые сообщения.

## Правила, которые легко нарушить

1. **`cleanup_ctx` требует async-генератор, а не `@asynccontextmanager`.**
   aiohttp вызывает `cb(app).__aiter__()`, поэтому декоратор ломает старт
   (`'_AsyncGeneratorContextManager' object has no attribute '__aiter__'`).
   В `main.py` `lifespan` — обычная async-функция с `yield`.

2. **`WEBHOOK_SECRET` обязательно хэшировать.** Telegram допускает в
   `secret_token` только `A-Za-z0-9_-`. `generateValue: true` в Render даёт
   base64 с `+` и `/`, и Telegram отвергает регистрацию: сервис «жив», `/health`
   отвечает, апдейты теряются. `_telegram_secret()` возвращает sha256-хэш.

3. **Webhook не удаляем при остановке.** Он принадлежит токену, а не процессу.
   При rollout новая инстанция стартует раньше, чем гаснет старая, и удаление
   оставило бы сервис без апдейтов. Для возврата к polling — `deleteWebhook`.

4. **`drop_pending_updates=False`.** Бесплатный хостинг спит; Telegram хранит
   апдейты 24 часа. С `True` сообщения, пришедшие во время сна, теряются.

5. **На Render без `DATABASE_URL` — отказ старта.** Диск стирается, SQLite
   потерял бы лиды. Проверка идёт по наличию `RENDER_EXTERNAL_URL`.

6. **Душится шум от `channel_binding`.** Neon добавляет этот параметр, asyncpg
   передаёт его серверу и получает ошибку. `split_ssl()` вырезает libpq-only
   параметры и возвращает флаг SSL.

7. **Заглушки в тестах должны иметь `session.close()`.** `SimpleRequestHandler`
   закрывает сессию при остановке; `FakeSession` в `tests/test_webhook.py`.

## Деплой

Neon (Postgres) + Render (webhook-сервис). Полная инструкция — `DEPLOY.md`,
конфигурация — `render.yaml`. Локально и на VPS — long polling без
`WEBHOOK_BASE_URL`.

### Что уже проверено (живой прогон)

- `main.py` поднимается с реальным Postgres и регистрирует webhook в Telegram.
- `/health` отдаёт `status: ok` и `storage: postgres`.
- Без заголовка `X-Telegram-Bot-Api-Secret-Token` webhook отвечает 401, с ним —
  200. Цепочка «апдейт → ключевое слово → запрос к LLM → запись в базу»
  проходит целиком; сбой LLM логируется и не роняет процесс.
- Тесты: 27 passed / 3 skipped без `TEST_DATABASE_URL`; с реальным Postgres —
  30 passed (проверено на Render Postgres 18, SSL).

### Инфраструктура Render

- Free-тариф Render разрешает **только одну** бесплатную базу. Она уже занята:
  `va-digital-db` (`dpg-dbfjrd67bikc73drd4ug-a`, oregon, PG 18, БД `vadigital`).
- Для изоляции создана отдельная БД `va_lead_radar` внутри этого инстанса —
  таблицы `chats`/`keywords`/`leads` не пересекаются с другими проектами.
  Имя БД подставляется вместо `vadigital` в конце строки подключения.
- Строку подключения даёт
  `GET /v1/postgres/<id>/connection-info` → `internalConnectionString`
  (для сервиса в Render) или `externalConnectionString` (снаружи).
  Пароль/хост в репозиторий не коммитить.

## Ограничение окружения

GitHub App `openhands-ai` (installation `175430587`) имеет `contents: write`,
но **не имеет `administration`**, поэтому `POST /user/repos` возвращает 403
`Resource not accessible by integration`. Репозиторий создаётся вручную, дальше
push идёт нормально. Проверено также: `gh` CLI авторизован тем же токеном (та же
ошибка), git credential helper обменивает JWT на токен, но прав это не меняет,
в браузере сессии GitHub нет (`logged_in=no`).
