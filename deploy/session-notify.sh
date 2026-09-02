#!/usr/bin/env bash
# Stop-хук: по завершении ответа Claude Code в WSL шлёт последний ответ
# ассистента в Telegram через мост (tgnotify --session) — с инлайн-кнопкой
# «▶️ Перейти к сессии» (= /resume <session-id> в боте).
#
# Срабатывает только если сессия идёт в TGBRIDGE_WORKDIR: только такие сессии
# резюмируемы через мост, для остальных кнопка бесполезна. Хук никогда не
# роняет ответ — любая осечка -> тихий exit 0.

set -uo pipefail

WORKDIR="${TGBRIDGE_WORKDIR:-$HOME/tgbridge}"
TGNOTIFY="$WORKDIR/.venv/bin/tgnotify"
[ -x "$TGNOTIFY" ] || exit 0

input=$(cat)

read -r -d '' PYCODE <<'PY' || true
import json, os, sys

MAXLEN = 1200

try:
    ev = json.loads(sys.stdin.read() or "{}")
except Exception:
    sys.exit(0)

if ev.get("stop_hook_active"):
    sys.exit(0)

workdir = os.path.realpath(os.environ["WORKDIR"])
if os.path.realpath(ev.get("cwd") or ".") != workdir:
    sys.exit(0)

sid = ev.get("session_id") or ""
tpath = ev.get("transcript_path") or ""
if not sid or not tpath or not os.path.exists(tpath):
    sys.exit(0)

last = ""
with open(tpath, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        parts = [
            b.get("text") or ""
            for b in (rec.get("message") or {}).get("content") or []
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if any(p.strip() for p in parts):
            last = "\n".join(parts).strip()

if not last:
    sys.exit(0)

one_line = " ".join(last.split())
if len(one_line) > MAXLEN:
    one_line = one_line[:MAXLEN] + " …"
print(sid)
print(one_line)
PY

payload=$(printf '%s' "$input" | WORKDIR="$WORKDIR" python3 -c "$PYCODE") || exit 0

[ -n "$payload" ] || exit 0
sid=$(printf '%s\n' "$payload" | sed -n 1p)
text=$(printf '%s\n' "$payload" | sed -n 2p)
[ -n "$sid" ] && [ -n "$text" ] || exit 0

cd "$WORKDIR" || exit 0
"$TGNOTIFY" --session "$sid" "$text" >/dev/null 2>&1 || true
exit 0
