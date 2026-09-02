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
