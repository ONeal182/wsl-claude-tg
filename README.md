# tgbridge

Мост `WSL (дом) ⇄ VPS (статический IP) ⇄ Telegram`.

- **Уведомления наружу:** `tgnotify "текст"` → VPS → Telegram (активно)
- **Промпты внутрь:** заменено на Claude Code **Remote Control** — в WSL по инстансу
  `claude-rc@<project>` (`claude remote-control --spawn worktree`) на каждый
  git-репозиторий в `$HOME`; на claude.ai/code проекты видны окружениями, новая
  сессия = git-worktree проекта. `tgbridge-rcsync` (таймер systemd) шлёт ссылки
  на окружения в бот, `/select_project` → выбор проекта отдаёт дип-линк на его
  окружение. Прежний путь «бот → VPS-очередь → агент → `claude -p`»
  (`src/tgbridge/agent/`) — legacy, из деплоя убран; код и тесты в дереве остаются.

Домашняя машина делает только исходящие запросы — белый IP и проброс портов не нужны.

## Компоненты

| Путь | Где работает | Что делает |
|---|---|---|
| `src/tgbridge/server/` | VPS | aiogram-бот (long-poll к Telegram) + FastAPI HTTP API + очередь SQLite |
| `src/tgbridge/agent/` | WSL | **legacy** long-poll к VPS + `claude -p`; в деплое заменён на `claude remote-control` |
| `src/tgbridge/cli/` | WSL | `tgnotify` — отправка уведомлений |

## Установка

```bash
uv sync --extra server --extra dev   # на VPS
uv sync --extra dev                  # в WSL (агенту хватает основных зависимостей)
cp .env.example .env                 # заполнить значения
```

## Запуск вручную

```bash
uv run tgbridge-server               # VPS
uv run tgbridge-agent                # WSL — legacy; в деплое: claude remote-control --spawn worktree (claude-rc@<project>)
uv run tgnotify "проверка"           # WSL
```

Продакшн-развёртывание (VPS + SSH-туннель + автозапуск) — см. **[deploy/DEPLOY.md](deploy/DEPLOY.md)**.

## Синхронность кода и документации

`deploy/hooks/docs-reminder.sh` — Stop-хук Claude Code. Если в рабочем дереве
менялся код, а `*.md` — нет, хук один раз просит обновить CLAUDE.md / README
перед завершением ответа (защита от цикла — по флагу `stop_hook_active`).

Подключение (в `~/.claude/settings.json`, в массив `hooks.Stop`):

```json
{ "hooks": [ { "type": "command", "command": "~/.claude/hooks/docs-reminder.sh", "timeout": 15 } ] }
```

Активен, только когда в корне git-репозитория есть `CLAUDE.md`.

## HTTP API (VPS)

Все ручки, кроме `/healthz`, требуют `Authorization: Bearer $TGBRIDGE_TOKEN`.

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| GET | `/healthz` | — | `{"ok": true}` |
| POST | `/notify` | `{"text": "...", "level": "info\|warn\|error"}` | `{"ok": true}` |
| GET | `/commands/next?timeout=25` | — | `200 {id, prompt, chat_id, fresh, resume_from}` или `204` |
| POST | `/commands/{id}/result` | `{"ok": true, "output": "...", "session_id": "..."}` | `{"ok": true}` |
