from __future__ import annotations

import os

import pytest

from tgbridge.config import Settings
from tgbridge.db import DB

TEST_TOKEN = "test-secret"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Тесты не должны цеплять реальный .env репозитория и хостовые TGBRIDGE_*."""
    for key in list(os.environ):
        if key.startswith("TGBRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # относительный env_file=".env" тут не найдётся


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        token=TEST_TOKEN,
        bot_token="",
        allowed_user_ids="466404679",
        db_path=str(tmp_path / "t.sqlite3"),
        server_url="http://testserver",
        claude_bin="claude",
        workdir=str(tmp_path),
        prompt_timeout=30,
    )


@pytest.fixture
def db(tmp_path) -> DB:
    d = DB(str(tmp_path / "queue.sqlite3"))
    yield d
    d.close()
