# CLAUDE.md — agent/

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Legacy.** В деплое этот агент заменён на `claude remote-control` (per-project,
> `deploy/claude-rc@.service` — см. корневой CLAUDE.md). Код и `tests/test_agent.py`
> в дереве остаются; правь их, только если сознательно возвращаешь long-poll поток.
> Контракт `models.py` не ломаем — сервер всё ещё умеет очередь.

Работает **в WSL на домашнем ПК**. Единственная связь с внешним миром — исходящий long-poll к VPS.

## Цикл (`loop` в `main.py`)

```
GET {server_url}/commands/next?timeout=25
  204            -> сразу следующий запрос
  200 {id,...}   -> run_prompt() -> POST /commands/{id}/result (с ретраями)
  сетевая ошибка -> sleep(backoff), backoff *= 2 до 60 c
```

Один `httpx.AsyncClient` на весь процесс (`base_url`, заголовок `Authorization`, `timeout=40` — заведомо больше серверного long-poll в 25 c).

## Особенности

- **`run_prompt(cfg, prompt, fresh, resume_from, cwd) -> (ok, вывод, session_id)`**: argv всегда `claude -p --output-format json`, дальше `resume_from` → `--resume <id> --fork-session` (перевешивает `fresh`), иначе `fresh` → ничего, иначе `--continue`; затем `asyncio.create_subprocess_exec(*argv, cwd=cwd or cfg.workdir)` (stdout/stderr раздельно), общий таймаут `cfg.prompt_timeout` → при превышении `proc.kill()`. `_parse_output()`: json → `.result`/`.session_id`/`.is_error`; не-json (краш до печати конверта) → сырой `stdout+stderr`, `session_id=""`. `FileNotFoundError` (нет бинаря `claude`) — обычный неуспешный результат, не роняет цикл. Все флаги (вкл. `cwd` выбранного проекта) приходят из `CommandOut` — решение принимает VPS, не агент.
- **`scan_projects(root)`** — не-скрытые (`name` без ведущей точки) подпапки первого уровня, `[(name, abspath)]` по алфавиту; пустой/битый `root` → `[]`. **`push_projects(client, cfg)`** шлёт их на `POST /projects` (best-effort, глотает `httpx.HTTPError`). `loop()` зовёт при старте и раз в `PROJECTS_SYNC_EVERY` c. Пустой `TGBRIDGE_PROJECTS_ROOT` → no-op.
- **Промпт не парсится и не экранируется** — уходит в `claude` одним argv-элементом (не через shell). Расширяя поведение, не собирай команду строкой.
- Результат режется по `MAX_OUTPUT` здесь. Сервер его больше не обрезает — рендерит в MarkdownV2 и бьёт на несколько сообщений; но верхний предел `MAX_OUTPUT` всё равно за агентом.
- Backoff сбрасывается в 1 после любого успешного ответа (в т.ч. `204`).
- Падение процесса безопасно: незакрытая задача на сервере через `LEASE_TTL` вернётся в очередь. Ретраи `POST result` — 5 попыток, дальше результат теряется (лог `результат #N потерян`).

## Зависимости

Только `httpx` из основного набора — extra `server` в WSL не ставится. Не импортируй `tgbridge.server.*` отсюда.
