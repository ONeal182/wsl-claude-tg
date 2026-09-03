# CLAUDE.md — server/

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Работает **на VPS**. Один процесс: FastAPI (HTTP API для WSL) + фоновый aiogram-бот (long-poll к Telegram).

> После перехода WSL на `claude remote-control` (см. корневой CLAUDE.md) активна только
> ветка `/notify` → Telegram. `prompt_reply()` в очередь больше не ставит — отвечает
> `BRIDGE_DISABLED_TEXT`; ручки `/commands/*`, `/projects` и команды сессий бота —
> legacy-контур (код и тесты в дереве, но никто не разбирает), трогаем осторожно.

## Порядок запуска (lifespan в `app.py`)

1. `DB(cfg.db_path)` — открыть/создать SQLite.
2. `wake = asyncio.Event()` — общий будильник для long-poll.
3. Если `cfg.bot_token` не пуст: `build_dispatcher()` + `asyncio.create_task(run_bot(...))`.
4. Всё кладётся на `app.state` (`db`, `wake`, `bot`) — хендлеры берут оттуда.
5. На остановке: отмена задачи бота → `bot.session.close()` → `db.close()`.

## Особенности

- **`/commands/next`** — long-poll руками: крутит `db.lease_next()`, между попытками ждёт `wake` с потолком 5 c, весь цикл ограничен `timeout` (клампится в 1..60). Бот вызывает `wake.set()` при новой задаче → агент получает её почти мгновенно. `CommandOut` несёт `cwd` (путь выбранного проекта или пусто).
- **`POST /projects`** — `{projects: [{name, path}]}` от агента → `db.sync_projects()` (INSERT OR IGNORE по `path`), ответ `{added: N}`. Заполняется `push_projects()` агента (скан `TGBRIDGE_PROJECTS_ROOT`).
- **`/notify`** шлёт во **все** `cfg.allowed_ids` через `send_md()`, ошибки отправки глушатся (`contextlib.suppress`). При непустом `NotifyIn.session_id` под последним сообщением — `resume_keyboard(session_id)` (та же кнопка «▶️ Перейти к сессии», что под результатом задачи). Заполняется Stop-хуком `session-notify.sh` в WSL.
- **`send_md(bot, chat_id, text, markup)`** (модульная функция в `app.py`) — единый путь отправки текста Claude: `render_md_chunks()` из `bot.py` (GFM → MarkdownV2 через `telegramify-markdown`, режется на куски ≤3900 по границам блоков, код-фенсы целы) → `bot.send_message(..., parse_mode="MarkdownV2")` по куску; `markup` только под последним. `TelegramBadRequest` на куске → повтор плоским текстом (`_strip_markdownv2`). Сервер вывод **не обрезает** (обрезка — на агенте, `MAX_OUTPUT`).
- Бот получает `db` и `wake` не импортом, а как kwargs `start_polling(bot, db=..., wake=...)`; в хендлерах они — обычные параметры (DI aiogram).
- Логика ответов бота — чистые функции `*_reply()` в `bot.py` (`id`, `prompt`, `new_session`, `resume`, `select_project`, `picked_project`, `sessions`, `history`); хендлеры aiogram только вызывают их и `msg.answer()`. `select_project_reply()` возвращает `(text, keyboard)` — хендлер шлёт `msg.answer(text, reply_markup=kb)`. Тесты бьют по чистым функциям + пара smoke-тестов через `dp.feed_raw_update` с перехватом `Message.answer`.
- `/select_project` (`select_project_reply`) → кнопки `project:<id>` из `db.projects()`; колбэк `on_project_cb` (`F.data.startswith("project:")`) → `picked_project_reply()` → `db.select_project(id)` (проект залипает в `session_state.project_id`, взводит `reset_pending`). Имя команды Telegram — только `[a-z0-9_]`, поэтому подчёркивание. `/sessions` теперь показывает `📂 <project_name>` (из `db.sessions()` LEFT JOIN).
- `/clear` (`clear_reply`) — `db.request_new_session()` взводит `reset_pending`, следующий `enqueue()` отдаёт задачу с `fresh=1` (чистый старт). `/new` (`new_reply`) — `db.request_resume(db.latest_session_id())`: следующая задача уедет с `resume_from=<последняя сессия>`, агент сделает `--resume <id> --fork-session` (контекст сохранён, новая ветка); пустой журнал сессий → фолбэк на `request_new_session()`. `/resume <id>` → `db.request_resume(id)` то же самое для явного id. `reset_pending` и `resume_id` взаимоисключающие, гасятся при `enqueue()`. Команды промпт в очередь **не** кладут. `/history` рендерит `db.history()`, `/sessions` — `db.sessions()` (id для `/resume` + title/last_result; превью 60 символов).
- **Журнал сессий:** `POST /commands/{id}/result` передаёт `body.session_id` в `db.finish()`; при непустом значении там же `record_session()` (upsert). Пустой `session_id` (агент не распознал json) — ничего не пишем.
- **Уведомление о результате:** `head` (`✅/❌ #id`) + `body.output` + при непустом `session_id` строка `🧩 сессия <id>`; всё это уходит через `send_md()` с `resume_keyboard(session_id)` под последним куском (▶️ Перейти к сессии, `callback_data="resume:<id>"`). Колбэк ловит `on_resume_cb` (`F.data.startswith("resume:")`) → `resume_reply()` → `cb.answer()` тостом. Кнопка = тот же эффект, что `/resume <id>`.
- Меню-кнопка бота в Telegram: `run_bot()` при старте зовёт `bot.set_my_commands(bot_commands())` из списка `COMMANDS`; ошибка этого вызова не роняет старт (только warning). Добавил команду — впиши её в `COMMANDS`.
- `handle_signals=False` у `start_polling` — сигналами управляет uvicorn/systemd, не бот.
- Тесты гоняются с `bot_token=""` → ветка «только API». Не ломай этот путь: не обращайся к `app.state.bot` без проверки на `None`.

## Зависимости

Ставятся extra `server` (`aiogram`, `fastapi`, `uvicorn`, `telegramify-markdown`). В WSL их нет — код отсюда там не импортируется.
