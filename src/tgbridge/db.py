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
    ok          INTEGER,
    fresh       INTEGER NOT NULL DEFAULT 0,
    resume_from TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
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

-- одна строка: ждёт ли следующий промпт старта новой сессии Claude.
-- Стартовое значение 1 -> самый первый промпт в базе идёт с чистого листа.
CREATE TABLE IF NOT EXISTS session_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    reset_pending INTEGER NOT NULL DEFAULT 1,
    resume_id     TEXT    NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO session_state (id, reset_pending) VALUES (1, 1);

-- журнал сессий Claude, отработавших через мост: id для /resume + что это было.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT    PRIMARY KEY,
    title       TEXT    NOT NULL DEFAULT '',
    last_result TEXT    NOT NULL DEFAULT '',
    turns       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
"""

_TITLE_MAX = 80


class DB:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Донакатить колонки на базах, созданных прежними версиями схемы."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(commands)")}
        if "fresh" not in cols:
            self._conn.execute(
                "ALTER TABLE commands ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0"
            )
        if "resume_from" not in cols:
            self._conn.execute(
                "ALTER TABLE commands ADD COLUMN resume_from TEXT NOT NULL DEFAULT ''"
            )
        st_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(session_state)")}
        if "resume_id" not in st_cols:
            self._conn.execute(
                "ALTER TABLE session_state ADD COLUMN resume_id TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        self._conn.close()

    # --- команды -----------------------------------------------------------

    def request_new_session(self) -> None:
        """Пометить, что следующий промпт стартует новую сессию Claude (/clear, /new)."""
        self._conn.execute(
            "UPDATE session_state SET reset_pending = 1, resume_id = '' WHERE id = 1"
        )
        self._conn.commit()

    def request_resume(self, session_id: str) -> None:
        """Следующий промпт продолжит указанную сессию Claude через --resume --fork-session."""
        self._conn.execute(
            "UPDATE session_state SET reset_pending = 0, resume_id = ? WHERE id = 1",
            (session_id,),
        )
        self._conn.commit()

    def enqueue(self, prompt: str, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT reset_pending, resume_id FROM session_state WHERE id = 1"
        ).fetchone()
        fresh = 1 if row and row["reset_pending"] else 0
        resume_from = row["resume_id"] if row else ""
        cur = self._conn.execute(
            "INSERT INTO commands (prompt, chat_id, fresh, resume_from, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (prompt, chat_id, fresh, resume_from, time.time()),
        )
        self._conn.execute(
            "UPDATE session_state SET reset_pending = 0, resume_id = '' WHERE id = 1"
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def history(self, limit: int = 10) -> list[sqlite3.Row]:
        """Последние задачи, новые сверху: id, prompt, status, ok, output, created_at."""
        return list(
            self._conn.execute(
                "SELECT id, prompt, status, ok, output, created_at FROM commands "
                "ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            )
        )

    def lease_next(self) -> CommandOut | None:
        """Вернуть в очередь протухшие leased, затем забрать одну задачу в работу."""
        now = time.time()
        self._conn.execute(
            "UPDATE commands SET status='queued', leased_at=NULL "
            "WHERE status='leased' AND leased_at < ?",
            (now - LEASE_TTL,),
        )
        row = self._conn.execute(
            "SELECT id, prompt, chat_id, fresh, resume_from FROM commands "
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
        return CommandOut(
            id=row["id"],
            prompt=row["prompt"],
            chat_id=row["chat_id"],
            fresh=bool(row["fresh"]),
            resume_from=row["resume_from"],
        )

    def finish(
        self, command_id: int, ok: bool, output: str, session_id: str = ""
    ) -> int | None:
        """Записать результат. Вернуть chat_id для ответа в Telegram или None."""
        row = self._conn.execute(
            "SELECT chat_id, prompt FROM commands WHERE id=? AND status='leased'",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE commands SET status='done', ok=?, output=?, done_at=? WHERE id=?",
            (1 if ok else 0, output, time.time(), command_id),
        )
        if session_id:
            self.record_session(session_id, prompt=row["prompt"], result=output)
        self._conn.commit()
        return int(row["chat_id"])

    # --- журнал сессий Claude --------------------------------------------

    def record_session(self, session_id: str, prompt: str, result: str) -> None:
        """Заапсертить сессию: title от первого промпта, потом только turns/last_result."""
        now = time.time()
        self._conn.execute(
            "INSERT INTO sessions (session_id, title, last_result, turns, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  last_result = excluded.last_result, "
            "  turns = sessions.turns + 1, "
            "  updated_at = excluded.updated_at",
            (session_id, prompt[:_TITLE_MAX], result, now, now),
        )
        self._conn.commit()

    def latest_session_id(self) -> str | None:
        """id самой свежей сессии Claude из журнала (для /new — форк текущей)."""
        row = self._conn.execute(
            "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row["session_id"] if row else None

    def sessions(self, limit: int = 15) -> list[sqlite3.Row]:
        """Сессии Claude, недавние сверху."""
        return list(
            self._conn.execute(
                "SELECT session_id, title, last_result, turns, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (max(1, limit),),
            )
        )

    # --- уведомления -----------------------------------------------------

    def log_notification(self, text: str, level: str) -> None:
        self._conn.execute(
            "INSERT INTO notifications (text, level, created_at) VALUES (?, ?, ?)",
            (text, level, time.time()),
        )
        self._conn.commit()
