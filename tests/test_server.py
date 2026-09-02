"""HTTP API сервера без реального Telegram (bot_token пуст -> ветка «только API»)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tgbridge.config import Settings
from tgbridge.server.app import create_app

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


def test_notify_with_session_id_attaches_resume_button(client):
    client._app.state.bot = AsyncMock()
    r = client.post(
        "/notify", json={"text": "задача завершена", "session_id": "sess-xyz"}, headers=AUTH
    )
    assert r.status_code == 200
    call = client._app.state.bot.send_message.await_args
    assert call.kwargs.get("parse_mode") == "MarkdownV2"
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "resume:sess-xyz"


def test_notify_without_session_id_has_no_button(client):
    client._app.state.bot = AsyncMock()
    client.post("/notify", json={"text": "просто уведомление"}, headers=AUTH)
    assert client._app.state.bot.send_message.await_args.kwargs.get("reply_markup") is None


def test_commands_next_empty_204(client):
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204


def test_queue_roundtrip(client):
    db = client._app.state.db
    db.enqueue(prompt="раскочегарь", chat_id=42)  # съедает стартовый fresh
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    cmd_id = db.enqueue(prompt="почини баг", chat_id=42)

    r = client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "id": cmd_id,
        "prompt": "почини баг",
        "chat_id": 42,
        "fresh": False,
        "resume_from": "",
    }

    # уже leased -> очередь пуста
    assert client.get("/commands/next", params={"timeout": 1}, headers=AUTH).status_code == 204

    r = client.post(
        f"/commands/{cmd_id}/result", json={"ok": True, "output": "готово"}, headers=AUTH
    )
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_commands_next_marks_fresh_after_new_session(client):
    db = client._app.state.db
    db.enqueue(prompt="p1", chat_id=1)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)  # p1 leased, fresh consumed
    db.request_new_session()
    db.enqueue(prompt="p2", chat_id=1)

    r = client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    assert r.json()["fresh"] is True


def test_commands_next_carries_resume_from(client):
    db = client._app.state.db
    db.enqueue(prompt="p1", chat_id=1)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    db.request_resume("sess-abc")
    db.enqueue(prompt="p2", chat_id=1)

    r = client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    assert r.json()["resume_from"] == "sess-abc"


def _text_of(call):
    return call.args[1] if len(call.args) > 1 else call.kwargs["text"]


def test_result_notification_shows_session_id_and_button(client):
    db = client._app.state.db
    cid = db.enqueue(prompt="p", chat_id=42)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    client._app.state.bot = AsyncMock()

    client.post(
        f"/commands/{cid}/result",
        json={"ok": True, "output": "готово", "session_id": "sess-abc"},
        headers=AUTH,
    )
    call = client._app.state.bot.send_message.await_args
    assert call.kwargs.get("parse_mode") == "MarkdownV2"
    assert "sess" in _text_of(call) and "сессия" in _text_of(call)
    kb = call.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "resume:sess-abc"


def test_result_notification_splits_long_output_button_on_last(client):
    db = client._app.state.db
    cid = db.enqueue(prompt="p", chat_id=42)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    client._app.state.bot = AsyncMock()

    big = "\n\n".join(f"Абзац {i} с текстом побольше для объёма." for i in range(400))
    client.post(
        f"/commands/{cid}/result",
        json={"ok": True, "output": big, "session_id": "sess-abc"},
        headers=AUTH,
    )
    calls = client._app.state.bot.send_message.await_args_list
    assert len(calls) > 1
    # кнопка — только под последним сообщением
    assert all(c.kwargs.get("reply_markup") is None for c in calls[:-1])
    assert calls[-1].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "resume:sess-abc"
    assert all(len(_text_of(c)) <= 4096 for c in calls)


def test_result_notification_no_button_without_session_id(client):
    db = client._app.state.db
    cid = db.enqueue(prompt="p", chat_id=42)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    client._app.state.bot = AsyncMock()

    client.post(
        f"/commands/{cid}/result", json={"ok": True, "output": "готово"}, headers=AUTH
    )
    call = client._app.state.bot.send_message.await_args
    assert call.kwargs.get("reply_markup") is None


def test_result_with_session_id_populates_sessions_table(client):
    db = client._app.state.db
    cid = db.enqueue(prompt="собери отчёт", chat_id=1)
    client.get("/commands/next", params={"timeout": 2}, headers=AUTH)
    r = client.post(
        f"/commands/{cid}/result",
        json={"ok": True, "output": "готово", "session_id": "sess-abc"},
        headers=AUTH,
    )
    assert r.status_code == 200
    rows = db.sessions()
    assert (rows[0]["session_id"], rows[0]["title"]) == ("sess-abc", "собери отчёт")


def test_result_unknown_id_ok_false(client):
    r = client.post("/commands/123/result", json={"ok": True, "output": "x"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": False}


def test_result_on_queued_not_leased_ok_false(client):
    cmd_id = client._app.state.db.enqueue(prompt="p", chat_id=1)
    r = client.post(f"/commands/{cmd_id}/result", json={"ok": False, "output": "x"}, headers=AUTH)
    assert r.json() == {"ok": False}
