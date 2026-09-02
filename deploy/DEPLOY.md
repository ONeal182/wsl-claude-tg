# Деплой

```
claude.ai/code / приложение Claude  ◄──►  claude remote-control  (WSL, дом)
                                                 (tgbridge-agent.service)

Telegram  ◄─── /notify ───  VPS: tgbridge-server (127.0.0.1:8090)
                                     ▲
                          SSH-туннель │ (поднимает WSL, tgbridge-tunnel)
                                     │
WSL (дом):  tgnotify / Stop-хук ─────┘
```

**Входящий поток теперь через Remote Control**, а не через бота: `tgbridge-agent.service`
держит `claude remote-control` (server mode), сессии видны и управляются с
claude.ai/code и из приложения Claude. Python-агент long-poll
(`src/tgbridge/agent`) — legacy, из деплоя убран (см. корневой CLAUDE.md).

Осталось от моста: **исходящие** уведомления — `tgnotify` и Stop-хук шлют на
`POST /notify` через SSH-туннель. Сервер на VPS наружу **не открыт** (только
`127.0.0.1:8090`). Бот на VPS ещё поднимается, но его очередь промптов больше
никто не разбирает — команды `/select_project`, `/sessions`, `/history`,
`/new`, `/resume` неактивны.

---

## 1. VPS (от root)

Скрипт самодостаточный — качается одним файлом, дальше клонирует репозиторий
в `/opt/tgbridge/app` сам. GitHub по SSH идёт через `ssh.github.com:443`
(порт 22 к GitHub с VPS закрыт) — это скрипт настраивает в `~tgbridge/.ssh/config`.

```bash
# доставить скрипт (с машины, где есть репозиторий):
git show HEAD:deploy/vps-setup.sh | ssh root@212.192.212.220 'cat > /root/vps-setup.sh'
# на VPS:
ssh root@212.192.212.220 'bash /root/vps-setup.sh'
```

Первый запуск создаёт юзера `tgbridge` и печатает **deploy-ключ** — добавь его
в GitHub: репозиторий → Settings → Deploy keys → Add deploy key (**без** write access).
Затем запусти скрипт ещё раз — он склонирует код и поставит зависимости.

После клонирования скрипт создаст `/opt/tgbridge/app/.env` — впиши в него:

| Переменная | Значение |
|---|---|
| `TGBRIDGE_TOKEN` | общий секрет (тот же, что в WSL) |
| `TGBRIDGE_BOT_TOKEN` | токен бота от @BotFather |
| `TGBRIDGE_ALLOWED_USER_IDS` | твой Telegram id |

`HOST=127.0.0.1`, `PORT=8090`, `DB_PATH=/opt/tgbridge/tgbridge.sqlite3` скрипт проставит сам.

```bash
systemctl restart tgbridge-server
journalctl -u tgbridge-server -f          # ждём "Run polling for bot @..."
curl -s http://127.0.0.1:8090/healthz     # {"ok":true}
```

Обновление в будущем: `cd /root/wsl-claude-tg && git pull && bash deploy/vps-setup.sh`.

---

## 2. WSL — домашняя машина (НЕ root)

Предпосылки: `systemd=true` в `/etc/wsl.conf`; `ssh root@212.192.212.220`
работает по ключу (проверено); `claude` в PATH и логин через claude.ai
(`claude /login` — не API-key, иначе Remote Control недоступен).

```bash
cd ~/tgbridge
git pull
bash deploy/wsl-setup.sh
```

Скрипт: `uv sync` (нужен для `tgnotify`), правит
`TGBRIDGE_SERVER_URL=http://127.0.0.1:8090` в `.env`, ставит и запускает два
user-юнита — `tgbridge-tunnel` и `tgbridge-agent` (= `claude remote-control`
в server mode), включает `linger` (стартуют вместе с WSL).

`tgbridge-agent` работает из `WorkingDirectory=%h` (`/home/oneal`): все сессии,
созданные с claude.ai, стартуют в этом cwd. Поменять — `WorkingDirectory=` в
`~/.config/systemd/user/tgbridge-agent.service` + `systemctl --user daemon-reload`.
`--spawn worktree` (в `ExecStart`) даёт каждой сессии свой git-worktree, если
cwd — репозиторий.

Проверка:

```bash
systemctl --user status tgbridge-tunnel tgbridge-agent
journalctl --user -u tgbridge-agent -n 30    # тут URL сессии wsl-home
curl -s http://127.0.0.1:8090/healthz        # {"ok":true} — туннель жив (для tgnotify)
```

Сессия `wsl-home` должна появиться в списке на claude.ai/code и во вкладке Code
приложения Claude. Оттуда её продолжаешь / создаёшь новые — код и файлы на
домашнем ПК, рулёжка с телефона или браузера.

### Stop-хук: результат интерактивной сессии в Telegram (опционально)

`deploy/session-notify.sh` по завершении ответа Claude Code в `TGBRIDGE_WORKDIR`
шлёт последний ответ ассистента через `tgnotify --session <id>` — в Telegram
приходит результат с кнопкой «▶️ Перейти к сессии». Ставится вручную:

```bash
mkdir -p ~/.claude/hooks
cp ~/tgbridge/deploy/session-notify.sh ~/.claude/hooks/session-notify.sh
chmod +x ~/.claude/hooks/session-notify.sh
```

Затем в `~/.claude/settings.json` в массив `hooks.Stop` добавить блок:

```json
{ "hooks": [ { "type": "command", "command": "~/.claude/hooks/session-notify.sh", "timeout": 20 } ] }
```

Хук читает `TGBRIDGE_WORKDIR` из окружения; если Claude Code запускается не через
systemd-юнит с `EnvironmentFile`, экспортни переменную в шелл-профиле или
поправь дефолт (`$HOME/tgbridge`) в самом скрипте.

---

## 3. Проверка сквозняка

Открой claude.ai/code (или вкладку Code в приложении Claude) → сессия `wsl-home`
на месте, отправь в неё сообщение → ответ приходит, файлы/команды идут на домашнем ПК.

`tgnotify "тест"` из WSL → сообщение в Telegram.
`tgnotify --session test-id "тест"` → сообщение с кнопкой «▶️ Перейти к сессии»
(сама кнопка сработает, только если сессия `test-id` реально лежит в `TGBRIDGE_WORKDIR`).

---

## Замечания по безопасности

- Туннель поднимается как `root@VPS`. Позже стоит завести на VPS отдельного
  юзера для форварда и ключ с `command="",permitopen="127.0.0.1:8090"` в
  `authorized_keys`.
- Единственный барьер для «промпт → claude -p на домашней машине» — allowlist
  по Telegram id и общий токен на HTTP API. Произвольный shell агент не исполняет.
