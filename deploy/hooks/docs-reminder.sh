#!/usr/bin/env bash
# Stop-хук: если в рабочем дереве менялся код, а документация — нет,
# один раз просит Claude обновить CLAUDE.md / README перед завершением ответа.
#
# Срабатывает только в git-репозитории, у которого в корне есть CLAUDE.md.
# Защита от цикла: при повторном (уже вызванном этим блоком) Stop во входе
# приходит "stop_hook_active": true -> выходим молча.

set -euo pipefail

input=$(cat)

case "$input" in
  *'"stop_hook_active": true'* | *'"stop_hook_active":true'*) exit 0 ;;
esac

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$root/CLAUDE.md" ] || exit 0

changed=$(git -C "$root" status --porcelain 2>/dev/null | cut -c4- | sed 's/.* -> //')
[ -n "$changed" ] || exit 0

code=$(printf '%s\n' "$changed" | grep -Ei '\.(py|ts|tsx|js|jsx|go|rs|rb|php|java|kt|c|h|cpp)$' || true)
[ -n "$code" ] || exit 0

docs=$(printf '%s\n' "$changed" | grep -Ei '\.(md|mdx|rst|adoc)$' || true)
[ -n "$docs" ] && exit 0

list=$(printf '%s\n' "$code" | paste -sd' ' -)
reason="Изменён код без правок документации: ${list}. Проверь корневой и модульные CLAUDE.md и README.md — обнови, если поменялись команды запуска/тестов, архитектура, контракт HTTP API (models.py) или инварианты очереди. Если документация не устарела — просто заверши ответ ещё раз (повторно этот хук не сработает)."

REASON="$reason" python3 -c 'import json,os; print(json.dumps({"decision":"block","reason":os.environ["REASON"]}, ensure_ascii=False))'
