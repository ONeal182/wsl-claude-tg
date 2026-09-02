#!/usr/bin/env bash
# Установка / обновление tgbridge server на VPS. Запускать от root:
#     bash deploy/vps-setup.sh
#
# Что делает: создаёт системного юзера tgbridge, клонирует репозиторий в
# /opt/tgbridge/app, ставит uv + Python 3.12 (в хоум юзера, не в систему),
# ставит зависимости, кладёт systemd-юнит, включает автозапуск.
#
# Чего НЕ трогает: ufw / iptables, Docker, VPN, сеть, apt, системный Python.
# Сервер слушает только 127.0.0.1:8090.
set -euo pipefail

REPO="${TGBRIDGE_REPO:-git@github.com:ONeal182/wsl-claude-tg.git}"
HOME_DIR=/opt/tgbridge
APP="$HOME_DIR/app"
UV="$HOME_DIR/.local/bin/uv"

[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }

# --- юзер ---
id tgbridge >/dev/null 2>&1 || \
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin tgbridge

# --- deploy-ключ для приватного репозитория ---
KEY="$HOME_DIR/.ssh/id_ed25519"
if [ ! -f "$KEY" ]; then
  install -d -o tgbridge -g tgbridge -m 700 "$HOME_DIR/.ssh"
  sudo -u tgbridge ssh-keygen -t ed25519 -N '' -f "$KEY" -C tgbridge-vps-deploy
  sudo -u tgbridge sh -c "ssh-keyscan -t ed25519 github.com >> '$HOME_DIR/.ssh/known_hosts'" 2>/dev/null
  cat <<EOF

============================================================
Добавь этот ключ в GitHub: репозиторий -> Settings -> Deploy keys
-> Add deploy key (галочку "Allow write access" НЕ ставь):

$(cat "$KEY.pub")

Потом запусти скрипт ещё раз.
============================================================
EOF
  exit 0
fi

# --- код ---
if [ -d "$APP/.git" ]; then
  sudo -u tgbridge git -C "$APP" fetch --prune origin
  sudo -u tgbridge git -C "$APP" reset --hard origin/master
else
  rm -rf "$APP"
  sudo -u tgbridge git clone "$REPO" "$APP"
fi

# --- uv (в хоум юзера) ---
if [ ! -x "$UV" ]; then
  sudo -u tgbridge env HOME="$HOME_DIR" sh -c \
    "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR='$HOME_DIR/.local/bin' INSTALLER_NO_MODIFY_PATH=1 sh"
fi

# --- зависимости + Python 3.12 (скачивает uv) ---
( cd "$APP" && sudo -u tgbridge env HOME="$HOME_DIR" "$UV" sync --extra server )

# --- .env ---
if [ ! -f "$APP/.env" ]; then
  sudo -u tgbridge cp "$APP/.env.example" "$APP/.env"
  sudo -u tgbridge sed -i \
    -e 's|^TGBRIDGE_HOST=.*|TGBRIDGE_HOST=127.0.0.1|' \
    -e 's|^TGBRIDGE_PORT=.*|TGBRIDGE_PORT=8090|' \
    -e 's|^TGBRIDGE_DB_PATH=.*|TGBRIDGE_DB_PATH=/opt/tgbridge/tgbridge.sqlite3|' \
    "$APP/.env"
  chmod 600 "$APP/.env"
  echo
  echo ">>> Заполни секреты в $APP/.env :"
  echo "    TGBRIDGE_TOKEN           — общий секрет (тот же, что в WSL)"
  echo "    TGBRIDGE_BOT_TOKEN       — токен бота от @BotFather"
  echo "    TGBRIDGE_ALLOWED_USER_IDS — твой Telegram id"
  echo ">>> потом:  systemctl restart tgbridge-server"
fi

# --- systemd ---
install -m 644 "$APP/deploy/tgbridge-server.service" /etc/systemd/system/tgbridge-server.service
systemctl daemon-reload
systemctl enable tgbridge-server >/dev/null 2>&1 || true

if [ -s "$APP/.env" ] && grep -q '^TGBRIDGE_BOT_TOKEN=.\+' "$APP/.env"; then
  systemctl restart tgbridge-server
  sleep 4
  echo "--- status ---";  systemctl is-active tgbridge-server
  echo "--- healthz ---"; curl -s -m 5 http://127.0.0.1:8090/healthz || echo "(нет ответа)"
  echo; echo "--- journal ---"; journalctl -u tgbridge-server -n 12 --no-pager
else
  echo ">>> .env не заполнен — сервис включён, но не стартует, пока не впишешь токены."
fi
