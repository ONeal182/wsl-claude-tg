"""Схемы полезной нагрузки HTTP API между агентом/CLI и сервером."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["info", "warn", "error"]


class NotifyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    level: Level = "info"


class CommandOut(BaseModel):
    """Задача, которую сервер отдаёт агенту."""

    id: int
    prompt: str
    chat_id: int


class ResultIn(BaseModel):
    """Результат выполнения промпта, который агент возвращает серверу."""

    ok: bool
    output: str = Field(default="", max_length=100_000)
