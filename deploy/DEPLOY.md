# Деплой

```
Telegram  ──long-poll──►  VPS: tgbridge-server (127.0.0.1:8090) + очередь SQLite
                                     ▲
                          SSH-туннель │ (поднимает WSL)
                                     │
WSL (дом):  tgbridge-tunnel ──► tgbridge-agent ──► claude -p
            tgnotify ──────────────────────────────► /notify
```

Сервер на VPS наружу **не открыт** — только `127.0.0.1:8090`. Агент из дома
приходит через SSH-туннель. ufw / Docker / VPN на VPS не трогаются.

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
работает по ключу (проверено).

```bash
cd ~/tgbridge
git pull
bash deploy/wsl-setup.sh
```

Скрипт: `uv sync`, правит `TGBRIDGE_SERVER_URL=http://127.0.0.1:8090` в `.env`,
ставит и запускает два user-юнита — `tgbridge-tunnel` и `tgbridge-agent`,
включает `linger` (стартуют вместе с WSL).

Проверка:

```bash
systemctl --user status tgbridge-tunnel tgbridge-agent
curl -s http://127.0.0.1:8090/healthz     # {"ok":true} — значит туннель жив
```

---

## 3. Проверка сквозняка

Отправь боту любой текст → должно прийти `⏳ принято, задача #N`, затем `✅ #N` с ответом.

`tgnotify "тест"` из WSL → сообщение в Telegram.

---

## Замечания по безопасности

- Туннель поднимается как `root@VPS`. Позже стоит завести на VPS отдельного
  юзера для форварда и ключ с `command="",permitopen="127.0.0.1:8090"` в
  `authorized_keys`.
- Единственный барьер для «промпт → claude -p на домашней машине» — allowlist
  по Telegram id и общий токен на HTTP API. Произвольный shell агент не исполняет.
