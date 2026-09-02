"""Telegram-бот на aiogram: long-poll к Telegram, приём промптов, allowlist.

Живёт в одном процессе с FastAPI (запускается как фоновая задача в lifespan).
Любое текстовое сообщение от разрешённого пользователя кладётся в очередь как задача.

Логика ответов вынесена в чистые функции (`*_reply`) — их и покрываем тестами;
хендлеры aiogram остаются тонкими обёртками.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from ..db import DB

log = logging.getLogger("tgbridge.bot")

# (команда, описание) — попадает в меню-кнопку бота через set_my_commands
COMMANDS: list[tuple[str, str]] = [
    ("new", "новая сессия — забыть контекст"),
    ("clear", "то же, что /new"),
    ("history", "последние задачи"),
    ("id", "показать твой Telegram id"),
    ("ping", "проверка связи"),
    ("start", "справка"),
]

START_TEXT = (
    "tgbridge на связи.\n"
    "Пришли текст — он уйдёт в WSL как промпт для Claude Code.\n"
    "Каждый следующий промпт продолжает тот же разговор.\n"
    "/clear, /new — начать новую сессию (забыть контекст)\n"
    "/history — последние задачи\n"
    "/id — показать твой Telegram id\n"
    "/ping — проверка"
)

HISTORY_LIMIT = 15
_PREVIEW = 60


def bot_commands() -> list[BotCommand]:
    """Список для меню-кнопки бота в Telegram."""
    return [BotCommand(command=c, description=d) for c, d in COMMANDS]


def id_reply(user_id: int, allowed_ids: set[int]) -> str:
    mark = "да" if user_id in allowed_ids else "НЕТ — добавь в TGBRIDGE_ALLOWED_USER_IDS"
    return f"твой id: `{user_id}`\nв allowlist: {mark}"


def new_session_reply(user_id: int, allowed_ids: set[int], db: DB) -> str:
    """/clear и /new: следующий промпт стартует новую сессию Claude с чистого листа."""
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    db.request_new_session()
    return "🧹 контекст очищен — следующий промпт начнёт новую сессию"


def _clip_preview(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= _PREVIEW else one_line[:_PREVIEW] + "…"


def _status_mark(status: str, ok: int | None) -> str:
    if status != "done":
        return "⏳"
    return "✅" if ok else "❌"


def history_reply(user_id: int, allowed_ids: set[int], db: DB, limit: int = HISTORY_LIMIT) -> str:
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    rows = db.history(limit)
    if not rows:
        return "история пуста"
    lines: list[str] = []
    for r in rows:
        when = time.strftime("%d.%m %H:%M", time.localtime(r["created_at"]))
        lines.append(f"#{r['id']} {_status_mark(r['status'], r['ok'])} {when}")
        lines.append(f"  → {_clip_preview(r['prompt'])}")
        if r["output"]:
            lines.append(f"  ← {_clip_preview(r['output'])}")
    return "\n".join(lines)


def prompt_reply(
    text: str, user_id: int, chat_id: int, allowed_ids: set[int], db: DB, wake: asyncio.Event
) -> str:
    """Обработать входящий текст: allowlist -> очередь -> разбудить long-poll агента."""
    if user_id not in allowed_ids:
        log.warning("отклонён промпт от чужого id=%s", user_id)
        return "не в allowlist, игнорирую"
    cmd_id = db.enqueue(prompt=text, chat_id=chat_id)
    wake.set()
    return f"⏳ принято, задача #{cmd_id}"


def build_dispatcher(allowed_ids: set[int]) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def on_start(msg: Message) -> None:
        await msg.answer(START_TEXT)

    @dp.message(Command("id"))
    async def on_id(msg: Message) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(id_reply(uid, allowed_ids), parse_mode="Markdown")

    @dp.message(Command("ping"))
    async def on_ping(msg: Message) -> None:
        await msg.answer("pong")

    @dp.message(Command("clear", "new"))
    async def on_new_session(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(new_session_reply(uid, allowed_ids, db))

    @dp.message(Command("history"))
    async def on_history(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(history_reply(uid, allowed_ids, db))

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(msg: Message, db: DB, wake: asyncio.Event) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(prompt_reply(msg.text, uid, msg.chat.id, allowed_ids, db, wake))

    return dp


async def run_bot(bot: Bot, dp: Dispatcher, db: DB, wake: asyncio.Event) -> None:
    log.info("старт polling Telegram")
    try:
        await bot.set_my_commands(bot_commands())
    except Exception as e:  # noqa: BLE001 — меню-кнопка не критична для старта
        log.warning("не удалось выставить меню команд: %s", e)
    await dp.start_polling(bot, db=db, wake=wake, handle_signals=False)
