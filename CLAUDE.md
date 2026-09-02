# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Мост `WSL (домашний ПК) ⇄ VPS (статический IP) ⇄ Telegram`. Два потока:

- **наружу:** `tgnotify` → `POST /notify` на VPS → Telegram
- **внутрь:** сообщение боту → очередь на VPS → агент в WSL забирает через long-poll → `claude -p --output-format json [--continue | --resume <id> --fork-session]` → `POST /commands/{id}/result` → ответ в Telegram

Промпты по умолчанию продолжают один разговор Claude (`--continue`). `/clear` и `/new` в боте помечают на VPS «следующий промпт — новая сессия»; `/resume <id>` — «следующий промпт продолжит сессию `<id>` в форке»; `/sessions` показывает пройденные через мост сессии Claude (id + title), `/history` — последние задачи. Ответ на каждую задачу содержит id сессии и инлайн-кнопку «Перейти к сессии» (= `/resume` этой сессии).

Домашний ПК делает **только исходящие** запросы: динамический IP и NAT не мешают. Всё, что должно пережить выключенный ПК (очередь промптов), лежит на VPS.

## Команды

```bash
uv sync --extra server --extra dev   # VPS: со всеми зависимостями бота
uv sync --extra dev                  # WSL: агенту хватает httpx из основных

uv run tgbridge-server               # запуск сервера (VPS)
uv run tgbridge-agent                # запуск агента (WSL)
uv run tgnotify "текст" [-l warn]    # разовое уведомление (WSL)

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
| `db.py` | `DB` — SQLite-очередь. Таблица `commands` как конечный автомат: `queued → leased → done`. `lease_next()` сначала возвращает в очередь protухшие `leased` (старше `LEASE_TTL`), потом атомарно забирает одну задачу. Таблица `session_state` (одна строка) — `reset_pending` + `resume_id`: `enqueue()` штампует ими `commands.fresh` / `commands.resume_from` и гасит оба; `request_new_session()` взводит `reset_pending` (и чистит `resume_id`), `request_resume(id)` — наоборот. Таблица `sessions` — журнал сессий Claude: `finish()` при непустом `session_id` зовёт `record_session()` (upsert, title = первый промпт, дальше только `turns`/`last_result`); `sessions(limit)` для `/sessions`. `history(limit)` — последние задачи. `_migrate()` донакатывает новые колонки на старые базы. Операции синхронные — база локальная |
| `server/app.py` | FastAPI. Бот запускается фоновой задачей в `lifespan`, объекты (`db`, `wake`, `bot`) висят на `app.state`. `/commands/next` — самодельный long-poll: цикл `lease_next()` + ожидание `asyncio.Event` (`wake`) до дедлайна, иначе `204` |
| `server/bot.py` | aiogram 3. `build_dispatcher(allowed_ids)` собирает хендлеры; `db` и `wake` прокидываются как kwargs в `start_polling` и приходят в хендлеры аргументами. Любой текст не-из-allowlist отклоняется; принятый — `db.enqueue()` + `wake.set()`. Команды `/clear`,`/new` → `db.request_new_session()`; `/resume <id>` → `db.request_resume(id)`; `/sessions` → `db.sessions()`; `/history` → `db.history()`. Ответы — чистые функции `*_reply()`. `run_bot()` при старте регистрирует меню-кнопку через `set_my_commands(bot_commands())` (список `COMMANDS`) |
| `server/auth.py` | зависимость FastAPI: `Authorization: Bearer <TGBRIDGE_TOKEN>`, сравнение через `secrets.compare_digest` |
| `agent/main.py` | бесконечный цикл long-poll к VPS с экспоненциальным backoff при обрыве. `run_prompt(cfg, prompt, fresh, resume_from) -> (ok, вывод, session_id)` запускает `claude -p --output-format json`: `resume_from` → `--resume <id> --fork-session` (перевешивает `fresh`), иначе `fresh` → чистый старт, иначе `--continue`. `_parse_output()` берёт `.result` / `.session_id` / `.is_error` из json, при нераспознанном json — сырой текст без id. Результат (+`session_id`) отдаётся серверу с ретраями |
| `cli/notify.py` | тонкий `httpx.post` на `/notify`, `-` в аргументе = читать stdin |

### Ключевые инварианты

- **Контракт API меняется только через `models.py`** — правишь схему, правишь обе стороны (`server/app.py` и `agent/main.py`).
- **`GET /commands/next` арендует задачу** (`leased`), не удаляет. Задача закрывается только успешным `POST /commands/{id}/result`. Если агент упал — через `LEASE_TTL` задача сама вернётся в очередь.
- **`server_url` / `token`** нужны и агенту, и CLI; **`bot_token` / `allowed_user_ids`** — только серверу. Пустой `bot_token` → сервер поднимает только HTTP API без бота (так гоняются тесты).
- Вывод `claude -p` (поле `.result` из json) режется дважды: `MAX_OUTPUT` в агенте, `MAX_TG_LEN` в сервере (лимит Telegram 4096).
- **Журнал сессий Claude пополняется только вперёд** — из `session_id`, который агент вытаскивает из `--output-format json` и кладёт в `ResultIn`. Существующие транскрипты `~/.claude` не импортируются.
- **Преемственность сессии живёт на VPS.** `commands.fresh` / `commands.resume_from` выставляются в момент `enqueue()` (снимок `reset_pending` / `resume_id`), едут агенту в `CommandOut` и решают: `--resume <id> --fork-session`, чистый старт или `--continue`. Первый промпт в свежей базе — всегда `fresh` (сид `reset_pending=1`). Оба флага одноразовые: `enqueue()` их гасит. Агент состояния сессии не хранит. `/resume` работает, только если целевая сессия лежит в `TGBRIDGE_WORKDIR` агента.

## Безопасность

Промпт из Telegram выполняется на домашней машине как `claude -p`. Единственный барьер — `allowed_ids`. Произвольный shell не исполняется намеренно. При расширении возможностей агента (свой список команд, доступ к файлам) — сначала прогонять `/security-review`.
