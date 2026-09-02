"""Единая конфигурация для всех трёх точек входа (server / agent / cli).

Значения берутся из переменных окружения или файла .env в корне проекта.
Каждая точка входа использует только свою часть полей — лишние просто игнорируются.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TGBRIDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- общее ---
    token: str = "change-me"  # общий секрет WSL <-> VPS

    # --- server (VPS) ---
    bot_token: str = ""
    allowed_user_ids: str = ""  # csv, парсится в allowed_ids
    db_path: str = "tgbridge.sqlite3"
    host: str = "0.0.0.0"
    port: int = 8080

    # --- agent + cli (WSL) ---
    server_url: str = "http://127.0.0.1:8080"
    claude_bin: str = "claude"
    workdir: str = "."
    prompt_timeout: int = Field(default=300, ge=10)

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x}


def load() -> Settings:
    return Settings()
