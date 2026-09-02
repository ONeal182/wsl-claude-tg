# CLAUDE.md — agent/

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

- **`run_prompt()`**: `asyncio.create_subprocess_exec(cfg.claude_bin, "-p", prompt, cwd=cfg.workdir)`, stderr слит в stdout, общий таймаут `cfg.prompt_timeout` → при превышении `proc.kill()`. `FileNotFoundError` (нет бинаря `claude`) отдаётся как обычный неуспешный результат, не роняет цикл.
- **Промпт не парсится и не экранируется** — уходит в `claude` одним argv-элементом (не через shell). Расширяя поведение, не собирай команду строкой.
- Результат режется по `MAX_OUTPUT` здесь и ещё раз на сервере — не полагайся на один слой.
- Backoff сбрасывается в 1 после любого успешного ответа (в т.ч. `204`).
- Падение процесса безопасно: незакрытая задача на сервере через `LEASE_TTL` вернётся в очередь. Ретраи `POST result` — 5 попыток, дальше результат теряется (лог `результат #N потерян`).

## Зависимости

Только `httpx` из основного набора — extra `server` в WSL не ставится. Не импортируй `tgbridge.server.*` отсюда.
