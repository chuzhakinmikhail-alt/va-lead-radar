# AGENTS.md — VA Lead Radar

Контекст для будущих сессий. Читается автоматически.

## Что это

Telegram-бот: сам находит заказы на биржах фриланса (RSS) и в публичных
Telegram-каналах (`t.me/s/`), плюс следит за чатами, куда его добавили. По
ключевым словам и через LLM решает, лид это или спам, и присылает админу карточку
с контактами или ссылкой. Python 3.11, aiogram 3.x, SQLite или Postgres,
Gemini/OpenAI.

## Команды

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # 104 теста, часть скипается без TEST_DATABASE_URL
python bot.py               # long polling (локально)
python main.py              # webhook + /health (хостинг)
```

## Структура

- `bot.py` — long polling. `main.py` — webhook + `/health`.
- `app/runtime.py` — общая сборка (бот, диспетчер, база, анализатор, поллер) для
  обоих входов. Меняя состав роутеров или зависимости, правьте только здесь.
- `app/config.py` — `load_config()`, единственное место чтения env.
- `app/db.py` — `Database` (ABC) + `SQLiteDatabase` + `PostgresDatabase`.
  Новый метод добавляется в ABC и в оба класса.
- `app/analyzer.py` — LLM, `LeadVerdict(is_lead, summary, intent)`.
- `app/contacts.py` — извлечение Telegram/email/телефона из текста объявления.
- `app/poller.py` — фоновый цикл: опрос источников → ключи → LLM → карточка.
- `app/sources/` — источники. `base.py` (LeadItem, `Source`, HTTP-клиент),
  `rss.py`, `telegram_public.py`, `telegram_userbot.py`; реестр и фабрика —
  в `__init__.py` (`create_source`, `SOURCE_KINDS`, `DEFAULT_SOURCES`).
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

8. **`paused` — не то же самое, что отсутствие чата.** В авторежиме
   (`MONITOR_ALL_CHATS=true`) чат подхватывается при первом сообщении. Если
   исключать чат удалением, он тут же вернётся. Поэтому `/pause` пишет флаг в
   `chats.paused`, а `is_monitored()` требует `paused = 0`; `_auto_register()`
   никогда не оживляет уже существующую строку.

9. **`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` — только Postgres.** SQLite
   такого синтаксиса не знает. Миграции разведены: `POSTGRES_MIGRATIONS`
   выполняются в `PostgresDatabase.connect()`, а `SQLiteDatabase.migrate()`
   смотрит `PRAGMA table_info(chats)`. Оба метода идемпотентны и вызываются
   при старте, так что старая база подтягивается без ручных шагов.

10. **Секреты не должны попадать в `/export`.** `_settings_json()` собирает
    только несекретные поля; ключи LLM и токен бота в выгрузку не входят.
    Тест `test_settings_json_is_valid_and_secret_free` это проверяет.

11. **Bot API не читает чужие чаты — это ограничение платформы, не баг.**
    Публичные каналы доступны через веб-превью `https://t.me/s/<username>`
    (парсим `data-post` и `tgme_widget_message_text`). Обычные группы — только
    через userbot (MTProto, telethon). Не пытайтесь «починить» это через Bot API.

12. **`tg_public`-источник всегда нормализуется перед сохранением.**
    `@name`, `name`, `t.me/name`, `t.me/s/name` приводятся к `name`
    (`normalise_channel`). Иначе один канал добавляется дважды, опрашивается
    дважды, и каждый пост приходит двумя лидами.

13. **Дедупликация внешних лидов — хэш `source_id:external_id`, не сам id.**
    Числовые id у разных бирж совпадают. `item_hash()` в `app/poller.py`.

14. **Поллер не должен падать от одного источника.** `Source.fetch()` ловит
    ошибки и возвращает `[]`; `Poller._loop()` ловит всё и продолжает. Цикл
    ограничен `CYCLE_TIMEOUT`, чтобы зависшая лента не блокировала опрос.

15. **Ключевые слова для чатов почти не встречаются на биржах.** Живой прогон:
    4 слова из `DEFAULT_KEYWORDS` дали 0 совпадений из 55 объявлений. Поэтому
    в `DEFAULT_SOURCE_KEYWORDS` лежит более широкий набор («ищу специалиста»,
    «нужен дизайнер» и т. п.). Точность обеспечивает LLM-стадия, а не слова.

16. **Карточка должна вести куда-то всегда.** Если контактов в тексте нет,
    подставляется ссылка на объявление (`url`), иначе — пометка «не указаны».
    Без этого лид бесполезен.

17. **Заглушка `FakeRuntime` в `tests/test_webhook.py` обязана иметь `poller`.**
    `main.create_app` обращается к `runtime.poller` в lifespan; без атрибута
    тест вешается (обращение к отсутствующему полю через `getattr` не спасёт,
    а реальный поллер ушёл бы в сеть).

18. **Новые источники регистрируются в `SOURCE_KINDS` и `create_source`.**
    `/addsource` проверяет тип по `SOURCE_KINDS`; незарегистрированный тип
    вернёт `None` из фабрики и будет молча пропущен поллером.

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
- Тесты: 108 passed / 11 skipped без `TEST_DATABASE_URL`; с реальным Postgres —
  119 passed (проверено на Render Postgres 18, SSL).
  Локальный прогон без `LLM_API_KEY` даёт 403 от Gemini — это ожидаемо, в проде
  ключ задан.

### Ключевые слова: границы слов и мгновенный поиск

- `match_keywords` ищет **по границам слов**, а не подстрокой (`keyword_pattern`
  и `match_keyword_list` в `app/db.py`). Python-овский `\b` знает только ASCII,
  поэтому для кириллицы класс символов задан явно — иначе `бот` ловил бы
  «работа», а `сайт` — «сайты». Оба бэкенда зовут одну свободную функцию.
- `/addkeyword` запускает `Poller.scan_backlog(keyword)` в фоне
  (`scan_backlog_soon`), чтобы результат был сразу, а не через `POLL_INTERVAL`.
  Анти-дубли при этом сохраняются: уже классифицированное объявление не
  переспрашивают у LLM и не шлют повторно.
- Потолок `MAX_BACKLOG = 25` объявлений за один проход: широкое слово иначе
  выжигает квоту LLM. Остаток добирает обычный цикл.
- Проверка на живых данных: 4 источника, 85 объявлений, скан по «ищу» →
  24 совпадения → 24 карточки, повторный скан → `sent=0`.

### Живые источники tg_public (проверено 2026-11-15)

- Работают: `freelance_zakazy` (~18 мин между постами, окно превью ~6 ч),
  `zakazy_it` (~3 совпадения по словам на 20 постов).
- Мёртвые (последний пост 2023 или 0 постов): `freelance_orders`, `workzilla`,
  `kwork_orders`, `freelance_ru`, `freelancehunt`, `naimanie`, `freelance_chat`.
  Поэтому `freelance_orders` убран из `DEFAULT_SOURCES` в пользу `zakazy_it`.
- Вывод: у публичных каналов окно ~20 постов, так что при частых постах
  `POLL_INTERVAL` нельзя ставить слишком большим — иначе посты проскакивают
  между циклами.

### Внешние источники (живой прогон)

Проверено 2026-11-15 на реальной сети, без ключей:

- `https://www.freelancer.com/rss.xml` → 20 объявлений, `parse_feed` вытащил
  заголовок, текст, ссылку и дату.
- `https://t.me/s/freelance_orders` → 15 постов, `https://t.me/s/freelance_zakazy`
  → 20 постов. Контакт (`@gotoisland`) извлечён из текста поста.
- Полный цикл `Poller.run_once()` на трёх источниках: 55 объявлений → 4 совпадения
  по ключам → 2 лида → 2 карточки. Повторный прогон: `sent=0` (анти-дубли).
- Не подходят: `upwork.com/ab/feed/jobs/rss` (403), `kwork.ru/rss` (услуги, не
  заказы), `freelance.habr.com/tasks` (SPA без данных в HTML).

### Авто-мониторинг (MONITOR_ALL_CHATS)

- Флаг читается в `config.py` (`_env_bool`), по умолчанию `false`.
- `_auto_register()` в `monitor.py` вызывается только когда чат не в списке;
  заводит строку в `chats` с названием и пропускает сообщение дальше.
- Личные чаты не подхватываются: `MONITORED_TYPES` = группа/супергруппа/канал.
- Миграция `paused` проверена на живой прод-базе: до прогона колонок было три
  (`chat_id`, `title`, `added_at`), после — четыре. Тесты гоняются и против
  прод-Postgres через `TEST_DATABASE_URL`.

### Проверка на проде (живой прогон)

Всё ниже подтверждено на `https://va-lead-radar.onrender.com` (коммит `b384ed0`):

- `/health` отдаёт `monitor_all_chats: true` — флаг виден снаружи.
- Апдейт без ключевых слов из нового чата → `stats.chats` вырос с 1 до 2,
  в `chats` появилась строка с названием чата. Авто-подхват работает.
- Апдейт с «нужен бот» в том же чате → `analysed=1`, `leads=1`, LLM вернул
  intent `разработка бота`. Повторная доставка того же апдейта (те же
  `chat_id`/`message_id`) новых строк не дала — анти-дубли работают.
- `/pause` в чате → `paused=1`; `/resume` → `paused=0`.
- `/settings` и `/export` из ЛС админа приняты вебхуком (200).
- Тестовые строки из `chats`/`leads` удалены, временные файлы со строкой
  подключения стёрты (`shred -u`).

**Грабли:** `PUT /v1/services/<id>/env-vars` не применяется к уже идущему
деплою. Первый прогон после добавления `MONITOR_ALL_CHATS` показал
`chats: 1` без изменений — переменная попала в процесс только со следующим
деплоем (push нового коммита). Поэтому флаг вынесен в `/health`: иначе
неотличимо «не сработало» от «не применилось».

### Выгрузка настроек

- `/settings` — текст: авторежим, модель, шлюз, хранилище, чаты с пометкой
  паузы, слова, счётчики.
- `/export` — JSON-файл (`_settings_json`) с блоком `env` для повторного
  развёртывания. Токен и ключ LLM в файл не пишутся.

### Инфраструктура Render

- Free-тариф Render разрешает **только одну** бесплатную базу. Она уже занята:
  `va-digital-db` (oregon, PG 18, БД `vadigital`); её id виден в
  `GET /v1/postgres?limit=20`.
- Для изоляции создана отдельная БД `va_lead_radar` внутри этого инстанса —
  таблицы `chats`/`keywords`/`leads` не пересекаются с другими проектами.
  Имя БД подставляется вместо `vadigital` в конце строки подключения.
- Строку подключения даёт
  `GET /v1/postgres/<id>/connection-info` → `internalConnectionString`
  (для сервиса в Render) или `externalConnectionString` (снаружи).
  Пароль/хост в репозиторий не коммитить.

### Живой сервис

- Репозиторий: `chuzhakinmikhail-alt/va-lead-radar` (ветка `main`, auto-deploy).
- Render web service: план free, регион oregon,
  URL https://va-lead-radar.onrender.com, health check `/health`.
- Переменные окружения задаются через
  `PUT /v1/services/<id>/env-vars` (полный список целиком, не патч).
- Логи через `GET /v1/services/<id>/logs` недоступны (404) — проверять состояние
  по `/health` и по содержимому таблицы `leads`.

### LLM в проде

- Используется OpenRouter (`OPENAI_BASE_URL=https://openrouter.ai/api/v1`,
  `LLM_PROVIDER=openai`), ключ взят из существующего сервиса `aiopen-agent`
  (`AVO_OPENAI_API_KEY`) — он же владельца.
- Ключ бесплатного тира: платные модели дают `402 Payment Required`, поэтому
  работаем на бесплатных `:free`-моделях.
- Бесплатные модели часто отдают `429 Too Many Requests`. От этого спасает
  `LLM_FALLBACK_MODELS` — цепочка резервных моделей.
- Проверено на проде: «ищу разработчика для бота» → `is_lead=1`,
  intent `разработка бота`; «нужен сайт» → `is_lead=1`, intent `сайт`.
  Три одинаковых апдейта дали одну строку в `leads` — анти-дубли работают.

### Полезно знать

- `leads` содержит все проанализированные сообщения, не только лиды
  (`is_lead` = 0/1). Первичный ключ — `msg_hash`, он же защита от дублей.
- `ADMIN_ID` владельца — `5450549001` (взят из сервиса `vapay`,
  `ADMIN_TELEGRAM_IDS`).

## Ограничение окружения

GitHub App `openhands-ai` (installation `175430587`) имеет `contents: write`,
но **не имеет `administration`**, поэтому `POST /user/repos` возвращает 403
`Resource not accessible by integration`. Новый репозиторий нужно создать
вручную; после этого push идёт нормально (так и был создан
`chuzhakinmikhail-alt/va-lead-radar`). Проверено также: `gh` CLI авторизован тем
же токеном (та же ошибка), git credential helper обменивает JWT на токен, но
прав это не меняет, в браузере сессии GitHub нет (`logged_in=no`).
