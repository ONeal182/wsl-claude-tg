# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Мост `WSL (домашний ПК) ⇄ VPS (статический IP) ⇄ Telegram`.

> **Входящий поток заменён на Claude Code Remote Control.** `tgbridge-agent.service`
> в WSL держит `claude remote-control` (server mode) — локальные сессии видны и
> управляются с claude.ai/code и из приложения Claude, код и файлы остаются на
> домашнем ПК. Python-агент long-poll (`src/tgbridge/agent`, точка входа
> `tgbridge-agent`) и разбор очереди ботом — **legacy**: код в дереве и тестах
> остаётся, из деплоя убран. `prompt_reply()` в очередь больше **не ставит** —
> отвечает `BRIDGE_DISABLED_TEXT` (ссылка на claude.ai/code); команды сессий
> (`/select_project`, `/new`, `/resume`, `/clear`) остались, но инертны — взводят
> `session_state`, который никто не читает. `/sessions`, `/history` — read-only
> журнал прошлых прогонов. Ниже описан мост целиком — как он в коде; активная
> часть сейчас — только исходящая (`/notify`) + Remote Control.

Потоки:

- **наружу (активно):** `tgnotify [--session <id>]` → `POST /notify` на VPS → Telegram (с `session_id` — плюс инлайн-кнопка «Перейти к сессии»). Сюда же Stop-хук.
- **внутрь (legacy):** сообщение боту → очередь на VPS → агент в WSL забирает через long-poll → `claude -p --output-format json [--continue | --resume <id> --fork-session]` (в `cwd` выбранного проекта или собственном `TGBRIDGE_WORKDIR`) → `POST /commands/{id}/result` → ответ в Telegram
- **проекты (legacy):** агент периодически сканирует `TGBRIDGE_PROJECTS_ROOT` (не-скрытые папки первого уровня) и шлёт список на `POST /projects`; `/select_project` в боте показывает их кнопками, выбор залипает в `session_state.project_id` и штампует `commands.cwd`

Stop-хук `~/.claude/hooks/session-notify.sh` (регистрируется в `~/.claude/settings.json`, **не** ставится `wsl-setup.sh`) по завершении интерактивной сессии Claude в `TGBRIDGE_WORKDIR` шлёт последний ответ ассистента через `tgnotify --session <id>` — в Telegram приходит результат с кнопкой резюма этой сессии.

Промпты по умолчанию продолжают один разговор Claude (`--continue`). `/clear` в боте помечает на VPS «следующий промпт — новая сессия с нуля» (`fresh`); `/new` — «форкнуть текущую» (= `/resume` последней сессии из журнала, контекст сохранён; если журнал пуст — то же, что `/clear`); `/resume <id>` — «следующий промпт продолжит сессию `<id>` в форке»; `/select_project` — список проектов из `TGBRIDGE_PROJECTS_ROOT` кнопками, нажатие делает проект текущим и стартует в нём новую сессию (`cwd`); `/sessions` показывает пройденные через мост сессии Claude (id + title + проект), `/history` — последние задачи. Ответ на каждую задачу содержит id сессии и инлайн-кнопку «Перейти к сессии» (= `/resume` этой сессии).

Журнал сессий (`sessions`) хранит `project_id` — из `commands.cwd` задачи, в которой сессия отработала. Таблица `projects` (`name` + `path`, `path` UNIQUE) наполняется двумя путями: `sync_projects()` от агентского скана и `finish()` лениво добавляет проект, если сессия отработала в неизвестном пути.

Домашний ПК делает **только исходящие** запросы: динамический IP и NAT не мешают. Всё, что должно пережить выключенный ПК (очередь промптов), лежит на VPS.

## Команды

```bash
uv sync --extra server --extra dev   # VPS: со всеми зависимостями бота
uv sync --extra dev                  # WSL: агенту хватает httpx из основных

uv run tgbridge-server               # запуск сервера (VPS)
uv run tgbridge-agent                # legacy long-poll агент (WSL); в деплое — claude remote-control
uv run tgnotify "текст" [-l warn] [--session <id>]   # разовое уведомление (WSL)

uv run pytest -q                     # все тесты
uv run pytest tests/test_db.py -q    # один модуль
uv run pytest tests/test_server.py::test_queue_roundtrip   # один тест
uv run ruff check --fix .            # линт
```

Развёртывание и автозапуск — **[deploy/DEPLOY.md](deploy/DEPLOY.md)** (VPS: `deploy/vps-setup.sh`; WSL: `deploy/wsl-setup.sh`).

## Разработка через тесты (TDD)

Правило: **никакого прод-кода без падающего теста впереди**. Цикл: красный → минимальная реализация → зелёный → весь `pytest` зелёный → коммит.

`tests/` зеркалит `src/tgbridge/` помодульно: `test_config`, `test_db`, `test_models`,
`test_auth`, `test_server`, `test_bot`, `test_agent`, `test_cli`. `tests/conftest.py`
даёт фикстуры `settings` / `db` и автозачистку `TGBRIDGE_*` + `chdir` (тест не должен
цеплять реальный `.env`). Логику, которую тяжело тестировать через фреймворк
(обработка сообщений бота), выносим в чистые функции — их и покрываем; хендлеры
aiogram / роуты FastAPI остаются тонкими.

## Архитектура

Один Python-пакет `tgbridge`, три точки входа делят общий код:

| Модуль | Роль |
|---|---|
| `config.py` | единый `Settings` (pydantic-settings, префикс `TGBRIDGE_`, читает `.env`). Каждая точка входа берёт только свою часть полей; `allowed_ids` парсит csv в `set[int]` |
| `models.py` | pydantic-схемы тела HTTP API — общий контракт между агентом и сервером |
| `db.py` | `DB` — SQLite-очередь. Таблица `commands` как конечный автомат: `queued → leased → done`. `lease_next()` сначала возвращает в очередь protухшие `leased` (старше `LEASE_TTL`), потом атомарно забирает одну задачу. Таблица `session_state` (одна строка) — `reset_pending` + `resume_id` + `project_id`: `enqueue()` штампует ими `commands.fresh` / `commands.resume_from` / `commands.cwd`, гасит `reset_pending`/`resume_id`, а `project_id` оставляет (проект залипает); `request_new_session()` взводит `reset_pending` (и чистит `resume_id`), `request_resume(id)` — наоборот, `select_project(id)` — ставит `project_id` + `reset_pending=1`. Таблица `sessions` — журнал сессий Claude: `finish()` при непустом `session_id` зовёт `record_session()` (upsert, title = первый промпт, дальше только `turns`/`last_result`; `project_id` из `_ensure_project(commands.cwd)`); `sessions(limit)` (LEFT JOIN `projects` → `project_name`) для `/sessions`, `latest_session_id()` для `/new` (форк последней). Таблица `projects` (`name`, `path` UNIQUE): `sync_projects(items)` — upsert от агентского скана, `_ensure_project(path)` — ленивая вставка из `finish()`, `projects(limit)` для `/select_project`. `history(limit)` — последние задачи. `_migrate()` донакатывает новые колонки на старые базы. Операции синхронные — база локальная |
| `server/app.py` | FastAPI. Бот запускается фоновой задачей в `lifespan`, объекты (`db`, `wake`, `bot`) висят на `app.state`. `/commands/next` — самодельный long-poll: цикл `lease_next()` + ожидание `asyncio.Event` (`wake`) до дедлайна, иначе `204`. `POST /projects` — `db.sync_projects()` от агента. Отправка в Telegram — `send_md()`: GFM → MarkdownV2 + разбивка на сообщения |
| `server/bot.py` | aiogram 3. `build_dispatcher(allowed_ids)` собирает хендлеры; `db` и `wake` прокидываются как kwargs в `start_polling` и приходят в хендлеры аргументами. Любой текст не-из-allowlist отклоняется; принятый — `db.enqueue()` + `wake.set()`. `/clear` → `db.request_new_session()` (чистый старт); `/new` → `db.request_resume(db.latest_session_id())` — форк последней сессии (пустой журнал → фолбэк на `request_new_session()`); `/resume <id>` → `db.request_resume(id)`; `/select_project` → `select_project_reply()` рисует кнопки `project:<id>` из `db.projects()`, колбэк → `picked_project_reply()` → `db.select_project(id)`; `/sessions` → `db.sessions()`; `/history` → `db.history()`. Ответы — чистые функции `*_reply()` (у `select_project_reply` — `(text, keyboard)`). `run_bot()` при старте регистрирует меню-кнопку через `set_my_commands(bot_commands())` (список `COMMANDS`). Имя команды — только `[a-z0-9_]` (Telegram), поэтому `/select_project`, не `/select-project` |
| `server/auth.py` | зависимость FastAPI: `Authorization: Bearer <TGBRIDGE_TOKEN>`, сравнение через `secrets.compare_digest` |
| `agent/main.py` | **legacy** (в деплое заменён на `claude remote-control` — см. врезку в «Что это»). Бесконечный цикл long-poll к VPS с экспоненциальным backoff при обрыве. `run_prompt(cfg, prompt, fresh, resume_from, cwd) -> (ok, вывод, session_id)` запускает `claude -p --output-format json` в `cwd or cfg.workdir`: `resume_from` → `--resume <id> --fork-session` (перевешивает `fresh`), иначе `fresh` → чистый старт, иначе `--continue`. `_parse_output()` берёт `.result` / `.session_id` / `.is_error` из json, при нераспознанном json — сырой текст без id. Результат (+`session_id`) отдаётся серверу с ретраями. `scan_projects(root)` — не-скрытые папки первого уровня; `push_projects()` шлёт их на `POST /projects` при старте и раз в `PROJECTS_SYNC_EVERY`. Код и `test_agent` в дереве остаются — контракт `models.py` не ломаем |
| `cli/notify.py` | тонкий `httpx.post` на `/notify`, `-` в аргументе = читать stdin; `--session <id>` кладёт `session_id` в тело (сервер вешает кнопку резюма) |

### Ключевые инварианты

- **Контракт API меняется только через `models.py`** — правишь схему, правишь обе стороны (`server/app.py` и `agent/main.py`).
- **`GET /commands/next` арендует задачу** (`leased`), не удаляет. Задача закрывается только успешным `POST /commands/{id}/result`. Если агент упал — через `LEASE_TTL` задача сама вернётся в очередь.
- **`server_url` / `token`** нужны и агенту, и CLI; **`bot_token` / `allowed_user_ids`** — только серверу. Пустой `bot_token` → сервер поднимает только HTTP API без бота (так гоняются тесты).
- Вывод `claude -p` (поле `.result` из json) агент режет по `MAX_OUTPUT`. Сервер его **не режет**: `send_md()` конвертирует GFM → MarkdownV2 (`telegramify-markdown`) и шлёт несколькими сообщениями, если не влезает в лимит Telegram (4096); инлайн-кнопка — под последним. Кусок, который Telegram отверг по разметке, уходит плоским текстом.
- **Журнал сессий Claude пополняется только вперёд** — из `session_id`, который агент вытаскивает из `--output-format json` и кладёт в `ResultIn`. Существующие транскрипты `~/.claude` не импортируются.
- **Преемственность сессии живёт на VPS.** `commands.fresh` / `commands.resume_from` / `commands.cwd` выставляются в момент `enqueue()` (снимок `reset_pending` / `resume_id` / путь `project_id`), едут агенту в `CommandOut` и решают: `--resume <id> --fork-session`, чистый старт или `--continue`, и в какой директории. Первый промпт в свежей базе — всегда `fresh` (сид `reset_pending=1`). `reset_pending` / `resume_id` одноразовые: `enqueue()` их гасит; `project_id` залипает до следующего `/select_project`. Агент состояния сессии не хранит. `/resume` и `--continue` работают, только если целевая сессия лежит в том же `cwd` (проекте или `TGBRIDGE_WORKDIR`).
- **Список проектов синхронит только агент.** `push_projects()` (скан `TGBRIDGE_PROJECTS_ROOT`) наполняет `projects` вперёд; `finish()` доливает проект, где реально отработала сессия. Пустой `TGBRIDGE_PROJECTS_ROOT` → синка нет, `/select_project` покажет только лениво добавленные проекты (или пусто).

## Безопасность

Активный контур сейчас — **исходящий**: `tgnotify` / Stop-хук → `POST /notify` → Telegram. Барьер на приём — bearer-токен на HTTP API и `allowed_ids` на боте.

Входящий контур (legacy `claude -p` из очереди) при возврате: промпт из Telegram выполняется на домашней машине как `claude -p`, единственный барьер — `allowed_ids`; произвольный shell не исполняется намеренно; при расширении возможностей агента — сначала `/security-review`.

`claude remote-control` в WSL открывает управление сессиями на домашней машине с claude.ai/приложения Claude — барьер здесь аккаунт claude.ai и (на Team/Enterprise) toggle Remote Control у Owner; на этой машине аккаунт личный (Pro).
