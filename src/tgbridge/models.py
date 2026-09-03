"""Схемы полезной нагрузки HTTP API между агентом/CLI и сервером."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["info", "warn", "error"]


class NotifyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    level: Level = "info"
    # непусто -> к сообщению цепляется кнопка «▶️ Перейти к сессии» (= /resume <id>).
    # Заполняет Stop-хук интерактивной сессии Claude в WSL.
    session_id: str = ""


class CommandOut(BaseModel):
    """Задача, которую сервер отдаёт агенту."""

    id: int
    prompt: str
    chat_id: int
    fresh: bool = False  # True -> агент стартует новую сессию Claude, иначе --continue
    # непусто -> агент делает `claude -p --resume <id> --fork-session` (перевешивает fresh)
    resume_from: str = ""
    # непусто -> агент запускает claude в этой директории (выбранный проект);
    # пусто -> берётся собственный TGBRIDGE_WORKDIR агента
    cwd: str = ""


class ProjectItem(BaseModel):
    """Один проект: имя папки + абсолютный путь к ней в WSL.

    `env_url` — ссылка `claude.ai/code?environment=...` сервера `claude-rc@<name>`
    (WSL шлёт её, когда remote-control поднят); пусто, если сервера нет.
    """

    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=2000)
    env_url: str = Field(default="", max_length=500)


class ProjectsIn(BaseModel):
    """Список проектов, который агент синхронизирует на сервер (POST /projects)."""

    projects: list[ProjectItem] = Field(default_factory=list, max_length=1000)


class ResultIn(BaseModel):
    """Результат выполнения промпта, который агент возвращает серверу."""

    ok: bool
    output: str = Field(default="", max_length=100_000)
    session_id: str = ""  # id сессии Claude из `--output-format json`, "" если не распознан
