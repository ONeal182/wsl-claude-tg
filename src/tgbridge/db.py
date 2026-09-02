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
    cwd         TEXT    NOT NULL DEFAULT '',
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
    resume_id     TEXT    NOT NULL DEFAULT '',
    project_id    INTEGER
);
INSERT OR IGNORE INTO session_state (id, reset_pending) VALUES (1, 1);

-- журнал сессий Claude, отработавших через мост: id для /resume + что это было.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT    PRIMARY KEY,
    title       TEXT    NOT NULL DEFAULT '',
    last_result TEXT    NOT NULL DEFAULT '',
    turns       INTEGER NOT NULL DEFAULT 0,
    project_id  INTEGER,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

-- проекты в WSL, в которых можно запускать сессию (/select-project).
-- Наполняется двумя путями: агент синхронит скан TGBRIDGE_PROJECTS_ROOT
-- (sync_projects) и finish() лениво добавляет проект, в котором отработала сессия.
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    path       TEXT    NOT NULL UNIQUE,
    created_at REAL    NOT NULL
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
        if "cwd" not in cols:
            self._conn.execute(
                "ALTER TABLE commands ADD COLUMN cwd TEXT NOT NULL DEFAULT ''"
            )
        st_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(session_state)")}
        if "resume_id" not in st_cols:
            self._conn.execute(
                "ALTER TABLE session_state ADD COLUMN resume_id TEXT NOT NULL DEFAULT ''"
            )
        if "project_id" not in st_cols:
            self._conn.execute("ALTER TABLE session_state ADD COLUMN project_id INTEGER")
        se_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "project_id" not in se_cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER")

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

    def select_project(self, project_id: int) -> sqlite3.Row | None:
        """/select-project: сделать проект текущим, следующий промпт — новая сессия в нём.

        Проект «залипает»: enqueue() штампует его путём в commands.cwd, пока не выберут
        другой. reset_pending взводится (чистый старт в новом проекте), resume_id гасится.
        """
        row = self._conn.execute(
            "SELECT id, name, path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE session_state SET project_id = ?, reset_pending = 1, resume_id = '' "
            "WHERE id = 1",
            (project_id,),
        )
        self._conn.commit()
        return row

    def enqueue(self, prompt: str, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT reset_pending, resume_id, project_id FROM session_state WHERE id = 1"
        ).fetchone()
        fresh = 1 if row and row["reset_pending"] else 0
        resume_from = row["resume_id"] if row else ""
        cwd = ""
        if row and row["project_id"]:
            p = self._conn.execute(
                "SELECT path FROM projects WHERE id = ?", (row["project_id"],)
            ).fetchone()
            cwd = p["path"] if p else ""
        cur = self._conn.execute(
            "INSERT INTO commands (prompt, chat_id, fresh, resume_from, cwd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (prompt, chat_id, fresh, resume_from, cwd, time.time()),
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
            "SELECT id, prompt, chat_id, fresh, resume_from, cwd FROM commands "
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
            cwd=row["cwd"],
        )

    def finish(
        self, command_id: int, ok: bool, output: str, session_id: str = ""
    ) -> int | None:
        """Записать результат. Вернуть chat_id для ответа в Telegram или None."""
        row = self._conn.execute(
            "SELECT chat_id, prompt, cwd FROM commands WHERE id=? AND status='leased'",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE commands SET status='done', ok=?, output=?, done_at=? WHERE id=?",
            (1 if ok else 0, output, time.time(), command_id),
        )
        if session_id:
            project_id = self._ensure_project(row["cwd"]) if row["cwd"] else None
            self.record_session(
                session_id, prompt=row["prompt"], result=output, project_id=project_id
            )
        self._conn.commit()
        return int(row["chat_id"])

    def _ensure_project(self, path: str) -> int | None:
        """Проект по пути: если такого нет — завести (name = имя папки). Вернуть id."""
        path = (path or "").strip()
        if not path:
            return None
        name = Path(path).name or path
        self._conn.execute(
            "INSERT OR IGNORE INTO projects (name, path, created_at) VALUES (?, ?, ?)",
            (name, path, time.time()),
        )
        got = self._conn.execute(
            "SELECT id FROM projects WHERE path = ?", (path,)
        ).fetchone()
        return int(got["id"]) if got else None

    # --- журнал сессий Claude --------------------------------------------

    def record_session(
        self, session_id: str, prompt: str, result: str, project_id: int | None = None
    ) -> None:
        """Заапсертить сессию: title от первого промпта, потом только turns/last_result.

        project_id ставится при первой записи; на апдейте — только если раньше был пуст.
        """
        now = time.time()
        self._conn.execute(
            "INSERT INTO sessions "
            "  (session_id, title, last_result, turns, project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  last_result = excluded.last_result, "
            "  turns = sessions.turns + 1, "
            "  project_id = COALESCE(sessions.project_id, excluded.project_id), "
            "  updated_at = excluded.updated_at",
            (session_id, prompt[:_TITLE_MAX], result, project_id, now, now),
        )
        self._conn.commit()

    def latest_session_id(self) -> str | None:
        """id самой свежей сессии Claude из журнала (для /new — форк текущей)."""
        row = self._conn.execute(
            "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row["session_id"] if row else None

    def sessions(self, limit: int = 15) -> list[sqlite3.Row]:
        """Сессии Claude, недавние сверху (+ имя проекта, если известно)."""
        return list(
            self._conn.execute(
                "SELECT s.session_id, s.title, s.last_result, s.turns, s.project_id, "
                "       s.created_at, s.updated_at, p.name AS project_name "
                "FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
                "ORDER BY s.updated_at DESC LIMIT ?",
                (max(1, limit),),
            )
        )

    # --- проекты --------------------------------------------------------

    def sync_projects(self, items: list[tuple[str, str]]) -> int:
        """Заапсертить список проектов (name, path). Вернуть, сколько добавлено новых."""
        now = time.time()
        added = 0
        for name, path in items:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO projects (name, path, created_at) VALUES (?, ?, ?)",
                (name, path, now),
            )
            added += cur.rowcount
        self._conn.commit()
        return added

    def projects(self, limit: int = 100) -> list[sqlite3.Row]:
        """Проекты для /select-project, по алфавиту."""
        return list(
            self._conn.execute(
                "SELECT id, name, path FROM projects ORDER BY name LIMIT ?",
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
