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
    clear_reply,
    history_reply,
    id_reply,
    new_reply,
    picked_project_reply,
    prompt_reply,
    render_md_chunks,
    resume_keyboard,
    resume_reply,
    run_bot,
    select_project_reply,
    sessions_reply,
)

ALLOWED = {466404679}


# --- чистые функции ---------------------------------------------------------


def test_id_reply_allowed():
    out = id_reply(466404679, ALLOWED)
    assert "466404679" in out and "да" in out


def test_id_reply_not_allowed():
    assert "НЕТ" in id_reply(999, ALLOWED)


def test_prompt_reply_points_to_web_and_does_not_enqueue(db: DB):
    """Мост в режиме remote-control: промпт боту не ставится в очередь,
    в ответ — подсказка открыть claude.ai/code."""
    wake = asyncio.Event()
    reply = prompt_reply("почини баг", 466404679, 555, ALLOWED, db, wake)
    assert "claude.ai/code" in reply
    assert not wake.is_set()
    assert db.lease_next() is None


def test_prompt_reply_rejects_stranger(db: DB):
    wake = asyncio.Event()
    reply = prompt_reply("rm -rf", 111, 111, ALLOWED, db, wake)
    assert "не в allowlist" in reply
    assert not wake.is_set()
    assert db.lease_next() is None


def test_clear_reply_sets_reset_pending(db: DB):
    out = clear_reply(466404679, ALLOWED, db)
    assert "сесси" in out.lower()
    db.enqueue("следующий", 1)
    assert db.lease_next().fresh is True


def test_clear_reply_rejects_stranger(db: DB):
    out = clear_reply(111, ALLOWED, db)
    assert "не в allowlist" in out
    db.enqueue("следующий", 1)
    db.lease_next()  # стартовый reset_pending
    db.enqueue("ещё", 1)
    assert db.lease_next().fresh is False  # чужой /clear не сбросил сессию


def test_new_reply_forks_latest_session(db: DB):
    db.record_session("sess-live", prompt="о чём говорили", result="о том о сём")
    db.enqueue("сид", 1)
    db.lease_next()  # съесть стартовый fresh
    out = new_reply(466404679, ALLOWED, db)
    assert "sess-live" in out
    db.enqueue("продолжаем", 1)
    got = db.lease_next()
    assert got.resume_from == "sess-live" and got.fresh is False


def test_new_reply_without_journal_falls_back_to_fresh(db: DB):
    db.enqueue("сид", 1)
    db.lease_next()  # съесть стартовый fresh
    out = new_reply(466404679, ALLOWED, db)
    assert "сесси" in out.lower()
    db.enqueue("первый настоящий", 1)
    got = db.lease_next()
    assert got.fresh is True and got.resume_from == ""


def test_new_reply_rejects_stranger(db: DB):
    db.record_session("sess-live", prompt="p", result="r")
    out = new_reply(111, ALLOWED, db)
    assert "не в allowlist" in out
    db.enqueue("сид", 1)
    db.lease_next()
    db.enqueue("ещё", 1)
    assert db.lease_next().resume_from == ""  # чужой /new не тронул сессию


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


def test_sessions_reply_lists_id_and_title(db: DB):
    db.record_session("b463918f-9e3d-4228-a202-5c6f7c0d6264", prompt="почини парсер", result="ок")
    out = sessions_reply(466404679, ALLOWED, db)
    assert "b463918f-9e3d-4228-a202-5c6f7c0d6264" in out
    assert "почини парсер" in out


def test_sessions_reply_shows_project_name(db: DB):
    db.sync_projects([("tgbridge", "/home/oneal/tgbridge")])
    pid = db.projects()[0]["id"]
    db.select_project(pid)
    cid = db.enqueue("почини парсер", 1)
    db.lease_next()
    db.finish(cid, ok=True, output="ок", session_id="sess-proj")
    out = sessions_reply(466404679, ALLOWED, db)
    assert "tgbridge" in out


def test_sessions_reply_empty(db: DB):
    assert "нет" in sessions_reply(466404679, ALLOWED, db).lower()


def test_sessions_reply_rejects_stranger(db: DB):
    assert "не в allowlist" in sessions_reply(111, ALLOWED, db)


def test_select_project_reply_lists_projects_as_buttons(db: DB):
    db.sync_projects([("tgbridge", "/home/oneal/tgbridge"), ("blog", "/home/oneal/blog")])
    text, kb = select_project_reply(466404679, ALLOWED, db)
    assert "проект" in text.lower()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["blog", "tgbridge"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(c.startswith("project:") for c in cbs)


def test_select_project_reply_empty_no_keyboard(db: DB):
    text, kb = select_project_reply(466404679, ALLOWED, db)
    assert kb is None and "пуст" in text.lower()


def test_select_project_reply_rejects_stranger(db: DB):
    text, kb = select_project_reply(111, ALLOWED, db)
    assert "не в allowlist" in text and kb is None


def test_picked_project_reply_selects_project(db: DB):
    db.sync_projects([("tgbridge", "/home/oneal/tgbridge")])
    pid = db.projects()[0]["id"]
    db.enqueue("сид", 1)
    db.lease_next()  # съесть стартовый fresh

    out = picked_project_reply(466404679, ALLOWED, db, str(pid))
    assert "tgbridge" in out

    db.enqueue("поехали", 1)
    got = db.lease_next()
    assert got.cwd == "/home/oneal/tgbridge" and got.fresh is True


def test_picked_project_reply_gives_env_link_when_present(db: DB):
    db.sync_projects(
        [("monorepo", "/home/oneal/monorepo", "https://claude.ai/code?environment=env_X")]
    )
    out = picked_project_reply(466404679, ALLOWED, db, str(db.projects()[0]["id"]))
    assert "https://claude.ai/code?environment=env_X" in out and "monorepo" in out


def test_picked_project_reply_without_env_url_points_at_server(db: DB):
    db.sync_projects([("blog", "/home/oneal/blog")])
    out = picked_project_reply(466404679, ALLOWED, db, str(db.projects()[0]["id"]))
    assert "claude-rc@blog" in out


def test_picked_project_reply_unknown_id(db: DB):
    out = picked_project_reply(466404679, ALLOWED, db, "999")
    assert "не найд" in out.lower()


def test_picked_project_reply_garbage_id(db: DB):
    out = picked_project_reply(466404679, ALLOWED, db, "abc")
    assert out  # не бросает


def test_picked_project_reply_rejects_stranger(db: DB):
    db.sync_projects([("tgbridge", "/home/oneal/tgbridge")])
    out = picked_project_reply(111, ALLOWED, db, str(db.projects()[0]["id"]))
    assert "не в allowlist" in out
    db.enqueue("p", 1)
    db.lease_next()
    db.enqueue("q", 1)
    assert db.lease_next().cwd == ""


def test_render_md_chunks_empty_input():
    assert render_md_chunks("") == []
    assert render_md_chunks("   \n  ") == []


def test_render_md_chunks_plain_text_one_chunk():
    chunks = render_md_chunks("просто короткий ответ")
    assert len(chunks) == 1 and "просто короткий ответ" in chunks[0]


def test_render_md_chunks_converts_bold_to_markdownv2():
    # GFM **bold** -> MarkdownV2 *bold*, а точка экранируется
    out = render_md_chunks("это **жирный** текст.")[0]
    assert "*жирный*" in out and "\\." in out


def test_render_md_chunks_keeps_code_fence_intact():
    out = render_md_chunks("вот код:\n\n```py\nx = 1\n```\n")[0]
    assert "```py\nx = 1\n```" in out


def test_render_md_chunks_splits_long_text_under_limit():
    raw = "\n\n".join(f"Абзац номер {i} с каким-то содержимым." for i in range(300))
    chunks = render_md_chunks(raw, limit=600)
    assert len(chunks) > 1
    assert all(len(c) <= 600 for c in chunks)
    # ничего не потеряли: первый и последний абзацы на месте
    assert "Абзац номер 0 " in chunks[0]
    assert "Абзац номер 299 " in chunks[-1]


def test_resume_keyboard_carries_session_callback():
    kb = resume_keyboard("9f5d203c-ca4c-4aab-b4b9-5ffb48deb907")
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "resume:9f5d203c-ca4c-4aab-b4b9-5ffb48deb907"
    assert btn.text


def test_resume_keyboard_none_without_session():
    assert resume_keyboard("") is None


def test_history_reply_rejects_stranger(db: DB):
    assert "не в allowlist" in history_reply(111, ALLOWED, db)


def test_bot_commands_cover_every_handler():
    names = {c.command for c in bot_commands()}
    assert {
        "start", "new", "clear", "resume", "sessions", "history", "id", "ping",
        "select_project",
    } <= names
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


async def test_dispatcher_text_from_allowed_points_to_web(bot, db: DB, answers):
    dp = build_dispatcher(ALLOWED)
    wake = asyncio.Event()
    await dp.feed_raw_update(bot, _raw("сделай отчёт", 466404679), db=db, wake=wake)
    assert answers and "claude.ai/code" in answers[0]
    assert not wake.is_set()
    assert db.lease_next() is None


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


async def test_dispatcher_new_forks_latest_session(bot, db: DB, answers):
    db.record_session("sess-live", prompt="p", result="r")
    db.enqueue("сид", 1)
    db.lease_next()  # съесть стартовый fresh
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/new", 466404679), db=db, wake=asyncio.Event())
    assert answers and "sess-live" in answers[0]
    db.enqueue("продолжаем", 1)
    assert db.lease_next().resume_from == "sess-live"


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


async def test_dispatcher_select_project_lists(bot, db: DB, monkeypatch):
    db.sync_projects([("tgbridge", "/home/oneal/tgbridge")])
    sent: list[tuple[str, object]] = []

    async def fake_answer(self, text, **kw):  # noqa: ANN001
        sent.append((text, kw.get("reply_markup")))

    monkeypatch.setattr("aiogram.types.Message.answer", fake_answer)
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(
        bot, _raw("/select_project", 466404679), db=db, wake=asyncio.Event()
    )
    assert sent and sent[0][1] is not None
    assert sent[0][1].inline_keyboard[0][0].callback_data.startswith("project:")


async def test_dispatcher_project_button_sends_env_link_as_message(bot, db: DB, monkeypatch):
    db.sync_projects(
        [("monorepo", "/home/oneal/monorepo", "https://claude.ai/code?environment=env_X")]
    )
    pid = db.projects()[0]["id"]
    msgs: list[str] = []
    acked: list[bool] = []

    async def fake_msg_answer(self, text, **kw):  # noqa: ANN001
        msgs.append(text)

    async def fake_cb_answer(self, text=None, **kw):  # noqa: ANN001
        acked.append(True)

    monkeypatch.setattr("aiogram.types.Message.answer", fake_msg_answer)
    monkeypatch.setattr("aiogram.types.CallbackQuery.answer", fake_cb_answer)
    upd = {
        "update_id": 3,
        "callback_query": {
            "id": "cb3",
            "chat_instance": "ci",
            "from": {"id": 466404679, "is_bot": False, "first_name": "T"},
            "data": f"project:{pid}",
            "message": {
                "message_id": 7,
                "date": int(time.time()),
                "chat": {"id": 466404679, "type": "private"},
            },
        },
    }
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, upd, db=db, wake=asyncio.Event())
    assert acked  # колбэк подтверждён (иначе кнопка «висит»)
    assert msgs and "https://claude.ai/code?environment=env_X" in msgs[0]
    db.enqueue("go", 1)
    assert db.lease_next().cwd == "/home/oneal/monorepo"


async def test_dispatcher_sessions_lists(bot, db: DB, answers):
    db.record_session("sess-777", prompt="сделай отчёт", result="готово")
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, _raw("/sessions", 466404679), db=db, wake=asyncio.Event())
    assert answers and "sess-777" in answers[0] and "сделай отчёт" in answers[0]


async def test_dispatcher_resume_button_pins_session(bot, db: DB, monkeypatch):
    toasts: list[str] = []

    async def fake_cb_answer(self, text=None, **kw):  # noqa: ANN001
        toasts.append(text or "")

    monkeypatch.setattr("aiogram.types.CallbackQuery.answer", fake_cb_answer)
    upd = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "chat_instance": "ci",
            "from": {"id": 466404679, "is_bot": False, "first_name": "T"},
            "data": "resume:sess-abc",
            "message": {
                "message_id": 5,
                "date": int(time.time()),
                "chat": {"id": 466404679, "type": "private"},
            },
        },
    }
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, upd, db=db, wake=asyncio.Event())
    assert toasts and "sess-abc" in toasts[0]
    db.enqueue("go", 1)
    assert db.lease_next().resume_from == "sess-abc"


async def test_dispatcher_resume_button_ignores_stranger(bot, db: DB, monkeypatch):
    async def fake_cb_answer(self, text=None, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr("aiogram.types.CallbackQuery.answer", fake_cb_answer)
    upd = {
        "update_id": 2,
        "callback_query": {
            "id": "cb2",
            "chat_instance": "ci",
            "from": {"id": 111, "is_bot": False, "first_name": "X"},
            "data": "resume:sess-abc",
            "message": {
                "message_id": 6,
                "date": int(time.time()),
                "chat": {"id": 111, "type": "private"},
            },
        },
    }
    dp = build_dispatcher(ALLOWED)
    await dp.feed_raw_update(bot, upd, db=db, wake=asyncio.Event())
    db.enqueue("go", 1)
    assert db.lease_next().resume_from == ""
