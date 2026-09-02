from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from tgbridge.db import DB
from tgbridge.server.bot import (
    bot_commands,
    build_dispatcher,
    history_reply,
    id_reply,
    new_session_reply,
    prompt_reply,
    resume_reply,
    run_bot,
)

ALLOWED = {466404679}


# --- чистые функции ---------------------------------------------------------


def test_id_reply_allowed():
    out = id_reply(466404679, ALLOWED)
    assert "466404679" in out and "да" in out


def test_id_reply_not_allowed():
    assert "НЕТ" in id_reply(999, ALLOWED)


def test_prompt_reply_enqueues_and_wakes(db: DB):
    wake = asyncio.Event()
    reply = prompt_reply("почини баг", 466404679, 555, ALLOWED, db, wake)
    assert reply == "⏳ принято, задача #1"
    assert wake.is_set()
    got = db.lease_next()
    assert (got.prompt, got.chat_id) == ("почини баг", 555)


def test_prompt_reply_rejects_stranger(db: DB):
    wake = asyncio.Event()
    reply = prompt_reply("rm -rf", 111, 111, ALLOWED, db, wake)
    assert "не в allowlist" in reply
    assert not wake.is_set()
    assert db.lease_next() is None


def test_new_session_reply_sets_reset_pending(db: DB):
    out = new_session_reply(466404679, ALLOWED, db)
    assert "сесси" in out.lower()
    db.enqueue("следующий", 1)
    assert db.lease_next().fresh is True


def test_new_session_reply_rejects_stranger(db: DB):
    out = new_session_reply(111, ALLOWED, db)
    assert "не в allowlist" in out
    db.enqueue("следующий", 1)
    db.lease_next()  # стартовый reset_pending
    db.enqueue("ещё", 1)
    assert db.lease_next().fresh is False  # чужой /new не сбросил сессию


def test_resume_reply_pins_next_command_to_session(db: DB):
    out = resume_reply(466404679, ALLOWED, db, "b463918f-9e3d-4228-a202-5c6f7c0d6264")
    assert "b463918f-9e3d-4228-a202-5c6f7c0d6264" in out
    db.enqueue("продолжаем", 1)
    got = db.lease_next()
    assert got.resume_from == "b463918f-9e3d-4228-a202-5c6f7c0d6264"


def test_resume_reply_without_id_shows_usage(db: DB):
    out = resume_reply(466404679, ALLOWED, db, "   ")
    assert "/resume" in out
    db.enqueue("p", 1)
    assert db.lease_next().resume_from == ""


def test_resume_reply_rejects_stranger(db: DB):
    out = resume_reply(111, ALLOWED, db, "sess-abc")
    assert "не в allowlist" in out
    db.enqueue("p", 1)
    assert db.lease_next().resume_from == ""


def test_history_reply_lists_recent_tasks(db: DB):
    a = db.enqueue("почини парсер даты", 1)
    db.lease_next()
    db.finish(a, ok=True, output="готово, добавил guard")
    db.enqueue("собери отчёт за неделю", 1)

    out = history_reply(466404679, ALLOWED, db)
    assert f"#{a}" in out and "#2" in out
    assert "почини парсер даты" in out and "собери отчёт за неделю" in out
    assert "✅" in out and "⏳" in out


def test_history_reply_empty(db: DB):
    assert "пуст" in history_reply(466404679, ALLOWED, db).lower()


def test_history_reply_rejects_stranger(db: DB):
    assert "не в allowlist" in history_reply(111, ALLOWED, db)


def test_bot_commands_cover_every_handler():
    names = {c.command for c in bot_commands()}
    assert {"start", "new", "clear", "resume", "history", "id", "ping"} <= names
    assert all(c.description for c in bot_commands())


async def test_run_bot_publishes_command_menu(db: DB):
    bot = AsyncMock()
    dp = AsyncMock()
    await run_bot(bot, dp, db, asyncio.Event())
    bot.set_my_commands.assert_awaited_once()
    published = {c.command for c in bot.set_my_commands.await_args.args[0]}
    assert {"history", "new", "clear"} <= published
    dp.start_polling.assert_awaited_once()


async def test_run_bot_survives_set_commands_failure(db: DB):
    bot = AsyncMock()
    bot.set_my_commands.side_effect = RuntimeError("telegram unreachable")
    dp = AsyncMock()
    await run_bot(bot, dp, db, asyncio.Event())  # не должно бросить
    dp.start_polling.assert_awaited_once()


# --- тонкая проверка проводки aiogram -------------------------------------


def _raw(text: str, uid: int, upd_id: int = 1) -> dict:
    return {
        "update_id": upd_id,
        "message": {
            "message_id": upd_id,
            "date": int(time.time()),
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "T"},
            "text": text,
        },
    }


@pytest.fixture
def bot() -> Bot:
    return AsyncMock(spec=Bot)


@pytest.fixture
def answers(monkeypatch) -> list[str]:
    """Перехватываем Message.answer — не завязываемся на биндинг бота в aiogram."""
    calls: list[str] = []

    async def fake_answer(self, text, **kw):  # noqa: ANN001
        calls.append(text)

    monkeypatch.setattr("aiogram.types.Message.answer", fake_answer)
    return calls


async def test_dispatcher_ping(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/ping", 466404679), db=db, wake=asyncio.Event())
    assert answers == ["pong"]


async def test_dispatcher_text_from_allowed_enqueues(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    wake = asyncio.Event()
    await dp.feed_raw_update(bot, _raw("сделай отчёт", 466404679), db=db, wake=wake)
    assert answers and "принято" in answers[0]
    assert wake.is_set()
    assert db.lease_next().prompt == "сделай отчёт"


async def test_dispatcher_text_from_stranger_rejected(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("привет", 111), db=db, wake=asyncio.Event())
    assert answers and "не в allowlist" in answers[0]
    assert db.lease_next() is None


async def test_dispatcher_clear_requests_new_session(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/clear", 466404679), db=db, wake=asyncio.Event())
    assert answers and "сесси" in answers[0].lower()
    db.enqueue("next", 1)
    assert db.lease_next().fresh is True


async def test_dispatcher_new_is_alias_of_clear(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/new", 466404679), db=db, wake=asyncio.Event())
    assert answers and "сесси" in answers[0].lower()


async def test_dispatcher_history_lists(bot, db: DB, answers):
    db.enqueue("задача раз", 1)
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/history", 466404679), db=db, wake=asyncio.Event())
    assert answers and "задача раз" in answers[0]


async def test_dispatcher_resume_pins_session(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(
        bot, _raw("/resume sess-abc", 466404679), db=db, wake=asyncio.Event()
    )
    assert answers and "sess-abc" in answers[0]
    db.enqueue("go", 1)
    assert db.lease_next().resume_from == "sess-abc"


async def test_dispatcher_resume_without_arg_shows_usage(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/resume", 466404679), db=db, wake=asyncio.Event())
    assert answers and "/resume" in answers[0]
