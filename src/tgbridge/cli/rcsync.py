"""tgbridge-rcsync — синхронизировать окружения claude-rc@<project> на VPS.

Для каждого юнита `claude-rc@<name>` в WSL берёт последнюю ссылку
`claude.ai/code?environment=...` из его journalctl и шлёт `{name, path, env_url}`
на `POST /projects`. Бот в `/select_project` отдаёт эту ссылку по выбору проекта.

Гоняется таймером systemd (--user) — см. deploy/tgbridge-rcsync.timer.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import httpx

from ..config import load

_ENV_RE = re.compile(r"https://claude\.ai/code\?environment=env_[A-Za-z0-9]+")
_UNIT_RE = re.compile(r"claude-rc@(?P<name>[^.\s]+)\.service")


def env_url_from_journal(text: str) -> str:
    """Последняя ссылка на окружение claude.ai/code в тексте журнала, иначе ''."""
    found = _ENV_RE.findall(text or "")
    return found[-1] if found else ""


def instances_from_units(text: str) -> list[str]:
    """Имена инстансов из вывода `systemctl list-units 'claude-rc@*'`, по порядку, без дублей."""
    seen: list[str] = []
    for line in (text or "").splitlines():
        m = _UNIT_RE.search(line)
        if m and m.group("name") not in seen:
            seen.append(m.group("name"))
    return seen


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def collect(home: Path) -> list[dict]:
    """[{name, path, env_url}] по всем юнитам claude-rc@<name> в WSL."""
    units = _run(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "claude-rc@*.service"]
    )
    projects: list[dict] = []
    for name in instances_from_units(units):
        journal = _run(
            ["journalctl", "--user", "-u", f"claude-rc@{name}.service", "-o", "cat", "--no-pager"]
        )
        projects.append(
            {"name": name, "path": str(home / name), "env_url": env_url_from_journal(journal)}
        )
    return projects


def main() -> int:
    projects = collect(Path.home())
    if not projects:
        return 0
    cfg = load()
    try:
        r = httpx.post(
            f"{cfg.server_url}/projects",
            headers={"Authorization": f"Bearer {cfg.token}"},
            json={"projects": projects},
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"tgbridge-rcsync: не доставлено: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
