# Развёртывание: Neon + Render

Бесплатный тариф Render даёт **web-сервис**, а не фоновый процесс, и стирает
файловую систему при каждом деплое. Поэтому бот работает в режиме webhook
(`main.py`), а данные лежат во внешнем Postgres (Neon). Ниже — весь путь.

## Что понадобится

| Что | Где взять | Бесплатно |
|---|---|---|
| Токен бота | [@BotFather](https://t.me/BotFather) | да |
| Свой Telegram ID | [@userinfobot](https://t.me/userinfobot) | да |
| Ключ LLM | [Google AI Studio](https://aistudio.google.com/apikey) | да |
| Postgres | [neon.tech](https://neon.tech) | да |
| Хостинг | [render.com](https://render.com) | да |

## Шаг 1. Отключите privacy mode у бота

Без этого Telegram не отдаёт боту обычные сообщения из групп, и мониторинг
молча ничего не находит.

@BotFather → `/mybots` → ваш бот → *Bot Settings* → *Group Privacy* →
*Turn off*. Затем удалите и заново добавьте бота в наблюдаемые чаты.

## Шаг 2. Создайте базу в Neon

1. Зарегистрируйтесь на [neon.tech](https://neon.tech) и создайте проект.
2. В панели проекта нажмите **Connect** и скопируйте строку вида
   `postgresql://user:password@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require`.
3. Сохраните её — это будущий `DATABASE_URL`.

Бот сам создаёт таблицы при первом запуске, миграции запускать не нужно.

> Строку Neon можно вставлять как есть, вместе с `channel_binding=require`:
> бот вырезает параметры, которые понимает только libpq, и сам включает SSL.

## Шаг 3. Выберите LLM

Подойдёт любой из двух вариантов.

**Gemini (проще всего).** Ключ из [Google AI Studio](https://aistudio.google.com/apikey).
`LLM_PROVIDER=gemini`, `LLM_API_KEY=<ключ>`. Модель по умолчанию —
`gemini-1.5-flash`.

**OpenAI или совместимый шлюз.** Ключ OpenAI, либо [OpenRouter](https://openrouter.ai)
(у него есть бесплатные модели). Тогда:

```
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_MODEL=nvidia/nemotron-3.5-lightning:free
```

> **Про бесплатные модели.** Они часто отвечают `429 Too Many Requests`. Бот
> это переживает: сообщение просто не станет лидом, в логе будет предупреждение.
> Чтобы лиды не терялись, задайте `LLM_FALLBACK_MODELS` — список моделей через
> запятую, которые пробуются по очереди:
>
> ```
> LLM_FALLBACK_MODELS=inclusionai/ling-3.2-flash:free,baidu/ernie-5.5-flash-preview:free,recursive/flint-v1.0:free
> ```
>
> Платные модели вроде `openai/gpt-4o-mini` надёжнее, но на бесплатном ключе
> OpenRouter вернут `402 Payment Required`.

## Шаг 4. Залейте код на GitHub

```bash
git init
git add .
git commit -m "VA Lead Radar"
git branch -M main
git remote add origin https://github.com/<ваш-логин>/va-lead-radar.git
git push -u origin main
```

## Шаг 5. Создайте сервис в Render

Проще всего через Blueprint: в `render.yaml` уже описаны команды запуска,
health check и переменные.

1. [dashboard.render.com](https://dashboard.render.com) → **New** →
   **Blueprint** → выберите репозиторий.
2. Render прочитает `render.yaml` и попросит заполнить переменные со
   `sync: false`:
   - `BOT_TOKEN` — токен от @BotFather;
   - `ADMIN_ID` — ваш Telegram ID;
   - `LLM_API_KEY` — ключ Gemini или OpenAI;
   - `DATABASE_URL` — строка из Neon (шаг 2).
3. Нажмите **Apply**. Render соберёт проект и запустит `python main.py`.

`WEBHOOK_BASE_URL` указывать не нужно: Render сам подставляет
`RENDER_EXTERNAL_URL` с публичным адресом сервиса, и `app/config.py` берёт его
автоматически. `WEBHOOK_SECRET` Render генерирует сам.

`MONITOR_ALL_CHATS` в `render.yaml` стоит `"false"`. Поставьте `"true"`, если
хотите, чтобы бот сам брал в работу любой чат, куда его добавили, без
`/addchat`. Лишние чаты отключайте командой `/pause` прямо в чате.

### Вариант вручную (без Blueprint)

**New → Web Service**, и укажите:

| Поле | Значение |
|---|---|
| Runtime | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python main.py` |
| Health Check Path | `/health` |
| Plan | Free |

Те же переменные окружения добавьте на вкладке *Environment*.

## Шаг 6. Проверьте, что всё работает

1. Откройте `https://<ваш-сервис>.onrender.com/health`. Должен вернуться JSON
   со `"status": "ok"`.
2. Проверьте, что Telegram знает про webhook:
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` — в `url` должен быть
   адрес вашего сервиса, а `last_error_message` отсутствовать.
3. Напишите боту `/start` — придёт справка.
4. Добавьте тестовый чат и напишите там фразу с ключевым словом. Админу должна
   прийти карточка лида.

## Шаг 7. Не давайте сервису заснуть

Бесплатный Render засыпает после ~15 минут без запросов. Проснувшись, он
подхватит накопившиеся апдейты (мы не удаляем их), но лучше держать его
активным. Настройте бесплатный внешний монитор:

- [cron-job.org](https://cron-job.org) или [UptimeRobot](https://uptimerobot.com)
- URL: `https://<ваш-сервис>.onrender.com/health`
- Интервал: каждые 5 минут

В репозитории есть и GitHub Action `.github/workflows/keepalive.yml`, но он
выключен по расписанию: на **приватном** репозитории такие запуски съедают
минуты GitHub Actions (лимит 2000 в месяц, каждый запуск округляется до минуты,
так что пинг каждые 10 минут — это ~4320 запусков и выход за лимит). Для
публичных репозиториев расписание можно раскомментировать.

## Обновление версии

Render пересобирает сервис на каждый push в ветку — отдельные команды не нужны.
`/health` возвращает `version` с хэшем коммита, так что видно, что именно
развёрнуто.

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| Сервис «Live», `/health` отвечает, но бот молчит | Webhook не зарегистрирован. Проверьте `getWebhookInfo` и логи Render. |
| `secret token contains illegal characters` | Значение `WEBHOOK_SECRET` содержит символы, недопустимые в Telegram. Бот хэширует секрет, поэтому такой ошибки быть не должно — убедитесь, что развёрнута актуальная версия `main.py`. |
| Бот не видит обычные сообщения в группе | Не отключён privacy mode (шаг 1). |
| `DATABASE_URL is not set` при старте | Бот отказывается работать на Render с SQLite: диск стирается, лиды потерялись бы. Добавьте `DATABASE_URL`. |
| Лиды приходят, но карточек нет | Не задан `ADMIN_ID`. В логе будет предупреждение. |
| `LLM analysis failed` | Неверный `LLM_API_KEY` или закончилась квота. Смотрите логи. |

## Переключение на long polling

Если разворачиваете на VPS или в Docker, просто не задавайте
`WEBHOOK_BASE_URL` и запускайте `python bot.py`. Учтите: перед этим снимите
webhook, иначе Telegram продолжит слать апдейты на старый адрес:

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```
