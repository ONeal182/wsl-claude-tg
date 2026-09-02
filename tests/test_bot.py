from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from tgbridge.db import DB
from tgbridge.server.bot import build_dispatcher, id_reply, prompt_reply

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
