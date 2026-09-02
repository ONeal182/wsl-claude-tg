# tgbridge

Мост `WSL (дом) ⇄ VPS (статический IP) ⇄ Telegram`.

- **Уведомления наружу:** `tgnotify "текст"` → VPS → Telegram
- **Промпты внутрь:** сообщение боту → VPS-очередь → агент в WSL → `claude -p` → ответ в Telegram

Домашняя машина делает только исходящие запросы — белый IP и проброс портов не нужны.

## Компоненты

| Путь | Где работает | Что делает |
|---|---|---|
| `src/tgbridge/server/` | VPS | aiogram-бот (long-poll к Telegram) + FastAPI HTTP API + очередь SQLite |
| `src/tgbridge/agent/` | WSL | long-poll к VPS, запуск `claude -p`, возврат результата |
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
uv run tgbridge-agent                # WSL
uv run tgnotify "проверка"           # WSL
```

Автозапуск — юниты в `deploy/` (см. комментарии внутри файлов).

## HTTP API (VPS)

Все ручки, кроме `/healthz`, требуют `Authorization: Bearer $TGBRIDGE_TOKEN`.

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| GET | `/healthz` | — | `{"ok": true}` |
| POST | `/notify` | `{"text": "...", "level": "info\|warn\|error"}` | `{"ok": true}` |
| GET | `/commands/next?timeout=25` | — | `200 {id, prompt, chat_id}` или `204` |
| POST | `/commands/{id}/result` | `{"ok": true, "output": "..."}` | `{"ok": true}` |
