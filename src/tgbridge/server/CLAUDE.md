# CLAUDE.md — server/

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Работает **на VPS**. Один процесс: FastAPI (HTTP API для WSL) + фоновый aiogram-бот (long-poll к Telegram).

## Порядок запуска (lifespan в `app.py`)

1. `DB(cfg.db_path)` — открыть/создать SQLite.
2. `wake = asyncio.Event()` — общий будильник для long-poll.
3. Если `cfg.bot_token` не пуст: `build_dispatcher()` + `asyncio.create_task(run_bot(...))`.
4. Всё кладётся на `app.state` (`db`, `wake`, `bot`) — хендлеры берут оттуда.
5. На остановке: отмена задачи бота → `bot.session.close()` → `db.close()`.

## Особенности

- **`/commands/next`** — long-poll руками: крутит `db.lease_next()`, между попытками ждёт `wake` с потолком 5 c, весь цикл ограничен `timeout` (клампится в 1..60). Бот вызывает `wake.set()` при новой задаче → агент получает её почти мгновенно.
- **`/notify`** шлёт во **все** `cfg.allowed_ids`, ошибки отправки глушатся (`contextlib.suppress`).
- Бот получает `db` и `wake` не импортом, а как kwargs `start_polling(bot, db=..., wake=...)`; в хендлерах они — обычные параметры (DI aiogram).
- Логика ответов бота — чистые функции `id_reply()` / `prompt_reply()` в `bot.py`; хендлеры aiogram только вызывают их и `msg.answer()`. Тесты бьют по чистым функциям + пара smoke-тестов через `dp.feed_raw_update` с перехватом `Message.answer`.
- `handle_signals=False` у `start_polling` — сигналами управляет uvicorn/systemd, не бот.
- Тесты гоняются с `bot_token=""` → ветка «только API». Не ломай этот путь: не обращайся к `app.state.bot` без проверки на `None`.

## Зависимости

Ставятся extra `server` (`aiogram`, `fastapi`, `uvicorn`). В WSL их нет — код отсюда там не импортируется.
