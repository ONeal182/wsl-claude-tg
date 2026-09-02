from __future__ import annotations

import time

from tgbridge import db as db_mod
from tgbridge.db import DB


def test_enqueue_returns_incrementing_ids(db: DB):
    assert db.enqueue("a", 1) == 1
    assert db.enqueue("b", 1) == 2


def test_lease_next_empty_returns_none(db: DB):
    assert db.lease_next() is None


def test_lease_next_is_fifo(db: DB):
    db.enqueue("first", 10)
    db.enqueue("second", 11)
    a = db.lease_next()
    b = db.lease_next()
    assert (a.prompt, a.chat_id) == ("first", 10)
    assert (b.prompt, b.chat_id) == ("second", 11)
    assert db.lease_next() is None  # обе в работе


def test_leased_task_not_handed_out_again(db: DB):
    db.enqueue("x", 1)
    assert db.lease_next() is not None
    assert db.lease_next() is None


def test_stale_lease_is_reclaimed(db: DB):
    cmd_id = db.enqueue("x", 1)
    leased = db.lease_next()
    assert leased.id == cmd_id
    # искусственно состарить аренду
    db._conn.execute(
        "UPDATE commands SET leased_at = ? WHERE id = ?",
        (time.time() - db_mod.LEASE_TTL - 1, cmd_id),
    )
    db._conn.commit()
    again = db.lease_next()
    assert again is not None and again.id == cmd_id


def test_finish_valid_returns_chat_id_and_closes(db: DB):
    cmd_id = db.enqueue("x", 777)
    db.lease_next()
    assert db.finish(cmd_id, ok=True, output="done") == 777
    row = db._conn.execute(
        "SELECT status, ok, output FROM commands WHERE id=?", (cmd_id,)
    ).fetchone()
    assert (row["status"], row["ok"], row["output"]) == ("done", 1, "done")


def test_finish_unknown_id_returns_none(db: DB):
    assert db.finish(999, ok=True, output="x") is None


def test_finish_not_leased_returns_none(db: DB):
    cmd_id = db.enqueue("x", 1)  # статус queued, не leased
    assert db.finish(cmd_id, ok=False, output="x") is None


def test_finish_twice_second_is_none(db: DB):
    cmd_id = db.enqueue("x", 1)
    db.lease_next()
    assert db.finish(cmd_id, ok=True, output="a") == 1
    assert db.finish(cmd_id, ok=True, output="b") is None


def test_first_ever_command_is_fresh(db: DB):
    db.enqueue("first", 1)
    assert db.lease_next().fresh is True


def test_subsequent_commands_are_not_fresh(db: DB):
    db.enqueue("first", 1)
    db.enqueue("second", 1)
    assert db.lease_next().fresh is True
    assert db.lease_next().fresh is False


def test_request_new_session_makes_next_command_fresh(db: DB):
    db.enqueue("first", 1)
    db.lease_next()  # съедает стартовый reset_pending
    db.enqueue("second", 1)
    assert db.lease_next().fresh is False

    db.request_new_session()
    db.enqueue("third", 1)
    db.enqueue("fourth", 1)
    assert db.lease_next().fresh is True
    assert db.lease_next().fresh is False


def test_reset_pending_persists_until_a_command_consumes_it(db: DB):
    db.enqueue("first", 1)
    db.lease_next()
    db.request_new_session()
    db.request_new_session()  # повторный /new ничего не ломает
    db.enqueue("next", 1)
    assert db.lease_next().fresh is True


def test_request_resume_stamps_next_command_only(db: DB):
    db.enqueue("warmup", 1)
    db.lease_next()

    db.request_resume("sess-xyz")
    db.enqueue("first after resume", 1)
    db.enqueue("second", 1)

    a = db.lease_next()
    b = db.lease_next()
    assert (a.resume_from, a.fresh) == ("sess-xyz", False)
    assert (b.resume_from, b.fresh) == ("", False)


def test_request_resume_clears_pending_new_session(db: DB):
    db.request_new_session()
    db.request_resume("sess-xyz")
    db.enqueue("p", 1)
    got = db.lease_next()
    assert got.resume_from == "sess-xyz" and got.fresh is False


def test_new_session_clears_pending_resume(db: DB):
    db.request_resume("sess-xyz")
    db.request_new_session()
    db.enqueue("p", 1)
    got = db.lease_next()
    assert got.resume_from == "" and got.fresh is True


def test_history_newest_first_with_status(db: DB):
    a = db.enqueue("prompt a", 1)
    b = db.enqueue("prompt b", 1)
    db.lease_next()  # a -> leased
    db.lease_next()  # b -> leased
    db.finish(a, ok=True, output="ответ a")
    db.finish(b, ok=False, output="ошибка b")
    c = db.enqueue("prompt c", 1)

    rows = db.history(limit=10)
    assert [r["id"] for r in rows] == [c, b, a]
    assert (rows[0]["status"], rows[0]["prompt"]) == ("queued", "prompt c")
    assert (rows[1]["status"], rows[1]["ok"], rows[1]["output"]) == ("done", 0, "ошибка b")
    assert (rows[2]["status"], rows[2]["ok"], rows[2]["output"]) == ("done", 1, "ответ a")


def test_history_respects_limit(db: DB):
    for i in range(5):
        db.enqueue(f"p{i}", 1)
    assert len(db.history(limit=3)) == 3


def test_record_session_first_time_sets_title_from_prompt(db: DB):
    db.record_session("sess-1", prompt="почини парсер даты", result="готово")
    row = db.sessions()[0]
    assert row["session_id"] == "sess-1"
    assert row["title"] == "почини парсер даты"
    assert row["last_result"] == "готово"
    assert row["turns"] == 1


def test_record_session_updates_keep_title_bump_turns(db: DB):
    db.record_session("sess-1", prompt="первый промпт", result="r1")
    db.record_session("sess-1", prompt="второй промпт", result="r2")
    row = db.sessions()[0]
    assert row["title"] == "первый промпт"
    assert row["last_result"] == "r2"
    assert row["turns"] == 2


def test_sessions_newest_first(db: DB):
    db.record_session("old", prompt="a", result="")
    db._conn.execute("UPDATE sessions SET updated_at = 1 WHERE session_id = 'old'")
    db.record_session("new", prompt="b", result="")
    assert [r["session_id"] for r in db.sessions()] == ["new", "old"]


def test_sessions_respects_limit(db: DB):
    for i in range(4):
        db.record_session(f"s{i}", prompt="p", result="")
    assert len(db.sessions(limit=2)) == 2


def test_latest_session_id_none_when_empty(db: DB):
    assert db.latest_session_id() is None


def test_latest_session_id_is_most_recently_updated(db: DB):
    db.record_session("old", prompt="a", result="")
    db._conn.execute("UPDATE sessions SET updated_at = 1 WHERE session_id = 'old'")
    db.record_session("fresh", prompt="b", result="")
    assert db.latest_session_id() == "fresh"


def test_finish_records_session_when_id_present(db: DB):
    cid = db.enqueue("собери отчёт", 1)
    db.lease_next()
    db.finish(cid, ok=True, output="отчёт собран", session_id="sess-xyz")
    row = db.sessions()[0]
    assert (row["session_id"], row["title"], row["last_result"]) == (
        "sess-xyz",
        "собери отчёт",
        "отчёт собран",
    )


def test_finish_without_session_id_records_nothing(db: DB):
    cid = db.enqueue("p", 1)
    db.lease_next()
    db.finish(cid, ok=True, output="out")
    assert db.sessions() == []


def test_log_notification_persists(db: DB):
    db.log_notification("hello", "warn")
    row = db._conn.execute("SELECT text, level FROM notifications").fetchone()
    assert (row["text"], row["level"]) == ("hello", "warn")


def test_schema_survives_reopen(tmp_path):
    path = str(tmp_path / "reopen.sqlite3")
    d1 = DB(path)
    d1.enqueue("keep", 5)
    d1.close()
    d2 = DB(path)
    got = d2.lease_next()
    assert got.prompt == "keep"
    d2.close()
