"""Сквозной тест HTTP API без реального Telegram (bot_token пуст)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tgbridge.config import Settings
from tgbridge.server.app import create_app

TOKEN = "test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    cfg = Settings(token=TOKEN, bot_token="", db_path=str(tmp_path / "t.sqlite3"))
    app = create_app(cfg)
    with TestClient(app) as c:
        c._app = app  # доступ к app.state.db в тестах
        yield c


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_auth_required(client):
    assert client.post("/notify", json={"text": "hi"}).status_code == 401
    assert client.get("/commands/next").status_code == 401


def test_notify_ok(client):
    r = client.post("/notify", json={"text": "сборка готова", "level": "warn"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_next_empty_returns_204(client):
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204


def test_queue_roundtrip(client):
    cmd_id = client._app.state.db.enqueue(prompt="почини баг", chat_id=42)

    r = client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"id": cmd_id, "prompt": "почини баг", "chat_id": 42}

    # повторный запрос — задача уже leased, очередь пуста
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204

    r = client.post(f"/commands/{cmd_id}/result", json={"ok": True, "output": "готово"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True}

    # повторный результат по закрытой задаче — ok=false
    r = client.post(f"/commands/{cmd_id}/result", json={"ok": True, "output": "x"}, headers=AUTH)
    assert r.json() == {"ok": False}
