"""HTTP API сервера без реального Telegram (bot_token пуст -> ветка «только API»)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tgbridge.config import Settings
from tgbridge.server.app import _clip, create_app

TEST_TOKEN = "test-secret"  # совпадает с conftest.TEST_TOKEN
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(tmp_path):
    cfg = Settings(
        token=TEST_TOKEN,
        bot_token="",
        allowed_user_ids="1",
        db_path=str(tmp_path / "s.sqlite3"),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        c._app = app
        yield c


# --- _clip ---------------------------------------------------------------


def test_clip_short_unchanged():
    assert _clip("abc") == "abc"


def test_clip_truncates_marker():
    out = _clip("x" * 5000)
    assert out.endswith("…(обрезано)") and len(out) < 5000


# --- эндпоинты ---------------------------------------------------------


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_auth_required(client):
    assert client.post("/notify", json={"text": "hi"}).status_code == 401
    assert client.get("/commands/next").status_code == 401
    assert client.post("/commands/1/result", json={"ok": True}).status_code == 401


def test_notify_ok_and_logged(client):
    r = client.post("/notify", json={"text": "сборка готова", "level": "warn"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = client._app.state.db._conn.execute(
        "SELECT text, level FROM notifications"
    ).fetchone()
    assert (row["text"], row["level"]) == ("сборка готова", "warn")


def test_notify_rejects_bad_body(client):
    assert client.post("/notify", json={"text": ""}, headers=AUTH).status_code == 422


def test_commands_next_empty_204(client):
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204


def test_queue_roundtrip(client):
    cmd_id = client._app.state.db.enqueue(prompt="почини баг", chat_id=42)

    r = client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"id": cmd_id, "prompt": "почини баг", "chat_id": 42}

    # уже leased -> очередь пуста
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204

    r = client.post(
        f"/commands/{cmd_id}/result", json={"ok": True, "output": "готово"}, headers=AUTH
    )
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_result_unknown_id_ok_false(client):
    r = client.post("/commands/123/result", json={"ok": True, "output": "x"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": False}


def test_result_on_queued_not_leased_ok_false(client):
    cmd_id = client._app.state.db.enqueue(prompt="p", chat_id=1)
    r = client.post(f"/commands/{cmd_id}/result", json={"ok": False, "output": "x"}, headers=AUTH)
    assert r.json() == {"ok": False}
