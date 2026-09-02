"""Очередь задач и журнал уведомлений на SQLite.

Одна таблица `commands` — конечный автомат по полю status:
    queued  -> задача принята от Telegram, ждёт агента
    leased  -> агент забрал в работу
    done    -> агент вернул результат (ok=1) или ошибку (ok=0)

`notifications` — просто история отправленных в Telegram сообщений.

SQLite-операции здесь синхронные: локальная база отвечает за микросекунды,
оборачивать в потоки смысла нет.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import CommandOut

# задача, зависшая в leased дольше этого времени, возвращается в очередь
LEASE_TTL = 600  # сек

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt     TEXT    NOT NULL,
    chat_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'queued',
    output     TEXT    NOT NULL DEFAULT '',
    ok         INTEGER,
    created_at REAL    NOT NULL,
    leased_at  REAL,
    done_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status, id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    level      TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
"""


class DB:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- команды -----------------------------------------------------------

    def enqueue(self, prompt: str, chat_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO commands (prompt, chat_id, created_at) VALUES (?, ?, ?)",
            (prompt, chat_id, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def lease_next(self) -> CommandOut | None:
        """Вернуть в очередь протухшие leased, затем забрать одну задачу в работу."""
        now = time.time()
        self._conn.execute(
            "UPDATE commands SET status='queued', leased_at=NULL "
            "WHERE status='leased' AND leased_at < ?",
            (now - LEASE_TTL,),
        )
        row = self._conn.execute(
            "SELECT id, prompt, chat_id FROM commands "
            "WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.commit()
            return None
        self._conn.execute(
            "UPDATE commands SET status='leased', leased_at=? WHERE id=?",
            (now, row["id"]),
        )
        self._conn.commit()
        return CommandOut(id=row["id"], prompt=row["prompt"], chat_id=row["chat_id"])

    def finish(self, command_id: int, ok: bool, output: str) -> int | None:
        """Записать результат. Вернуть chat_id для ответа в Telegram или None."""
        row = self._conn.execute(
            "SELECT chat_id FROM commands WHERE id=? AND status='leased'",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE commands SET status='done', ok=?, output=?, done_at=? WHERE id=?",
            (1 if ok else 0, output, time.time(), command_id),
        )
        self._conn.commit()
        return int(row["chat_id"])

    # --- уведомления -----------------------------------------------------

    def log_notification(self, text: str, level: str) -> None:
        self._conn.execute(
            "INSERT INTO notifications (text, level, created_at) VALUES (?, ?, ?)",
            (text, level, time.time()),
        )
        self._conn.commit()
