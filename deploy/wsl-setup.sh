#!/usr/bin/env bash
# Установка в WSL (домашняя машина). НЕ от root:
#     bash deploy/wsl-setup.sh
#
# Ставит user-юниты systemd: tgbridge-tunnel (ssh -L до VPS, нужен для
# tgnotify/Stop-хука) и claude-rc@<project> — по инстансу `claude remote-control`
# (server mode, --spawn worktree) на каждый git-репозиторий первого уровня в $HOME.
# Сессии видны/управляются с claude.ai/code, новая сессия = git-worktree проекта.
#
# Требует: `claude` в PATH и логин через claude.ai (`claude /login`).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

command -v uv >/dev/null || { echo "нет uv в PATH"; exit 1; }
command -v claude >/dev/null || { echo "нет claude в PATH"; exit 1; }
[ "$(id -u)" != 0 ] || { echo "не запускай от root"; exit 1; }

# tgnotify (cli/notify.py) и Stop-хук берут httpx из основного набора
uv sync

# --- .env (нужно только tgnotify/Stop-хуку: адрес VPS через туннель) ---
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
sed -i 's|^TGBRIDGE_SERVER_URL=.*|TGBRIDGE_SERVER_URL=http://127.0.0.1:8090|' .env
echo ">>> .env: TGBRIDGE_TOKEN должен совпадать со значением на VPS"
grep -E '^TGBRIDGE_(TOKEN|SERVER_URL|WORKDIR)=' .env | sed 's/\(TOKEN=\).*/\1***/'

# --- claude remote-control требует логин через claude.ai (не API-key) ---
claude auth status 2>/dev/null | grep -q '"authMethod": "claude.ai"' \
  || echo ">>> залогинься: claude /login (нужен claude.ai-аккаунт для remote-control)"

# --- systemd user units ---
mkdir -p ~/.config/systemd/user
cp deploy/tgbridge-tunnel.service 'deploy/claude-rc@.service' ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user enable --now tgbridge-tunnel.service
sleep 3

# по инстансу claude-rc на каждый git-репозиторий первого уровня в $HOME
shopt -s nullglob
projects=()
for d in "$HOME"/*/; do
  [ -d "${d}.git" ] && projects+=("$(basename "$d")")
done
if [ ${#projects[@]} -eq 0 ]; then
  echo ">>> в $HOME нет git-репозиториев — включи вручную: systemctl --user enable --now claude-rc@<project>"
else
  for p in "${projects[@]}"; do
    systemctl --user enable --now "claude-rc@$p.service"
  done
  sleep 3
  for p in "${projects[@]}"; do
    echo "--- claude-rc@$p: $(systemctl --user is-active "claude-rc@$p") ---"
  done
fi

echo "--- tunnel: $(systemctl --user is-active tgbridge-tunnel) ---"
echo -n "--- healthz через туннель: "
curl -s -m 5 http://127.0.0.1:8090/healthz || echo "(нет ответа — запущен ли сервер на VPS?)"
echo
echo "логи + URL сессии:  journalctl --user -u claude-rc@<project> -f"
echo "проекты появятся окружениями на claude.ai/code — новая сессия = git-worktree проекта"
