#!/usr/bin/env bash
# Установка агента + SSH-туннеля в WSL (домашняя машина). НЕ от root:
#     bash deploy/wsl-setup.sh
#
# Ставит два user-юнита systemd: tgbridge-tunnel (ssh -L до VPS) и
# tgbridge-agent (забирает промпты, запускает claude -p).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

command -v uv >/dev/null || { echo "нет uv в PATH"; exit 1; }
[ "$(id -u)" != 0 ] || { echo "не запускай от root"; exit 1; }

uv sync

# --- .env ---
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
sed -i 's|^TGBRIDGE_SERVER_URL=.*|TGBRIDGE_SERVER_URL=http://127.0.0.1:8090|' .env
echo ">>> .env: TGBRIDGE_TOKEN должен совпадать со значением на VPS"
grep -E '^TGBRIDGE_(TOKEN|SERVER_URL|WORKDIR|CLAUDE_BIN|PROJECTS_ROOT)=' .env | sed 's/\(TOKEN=\).*/\1***/'

# --- systemd user units ---
mkdir -p ~/.config/systemd/user
cp deploy/tgbridge-tunnel.service deploy/tgbridge-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user enable --now tgbridge-tunnel.service
sleep 3
systemctl --user enable --now tgbridge-agent.service
sleep 3

echo "--- tunnel: $(systemctl --user is-active tgbridge-tunnel) ---"
echo "--- agent:  $(systemctl --user is-active tgbridge-agent) ---"
echo -n "--- healthz через туннель: "
curl -s -m 5 http://127.0.0.1:8090/healthz || echo "(нет ответа — запущен ли сервер на VPS?)"
echo
echo "логи:  journalctl --user -u tgbridge-agent -f"
