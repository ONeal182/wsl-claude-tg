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

import telegramify_markdown
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..db import DB

log = logging.getLogger("tgbridge.bot")

# (команда, описание) — попадает в меню-кнопку бота через set_my_commands
COMMANDS: list[tuple[str, str]] = [
    ("new", "новая сессия, контекст сохранить (форк текущей)"),
    ("clear", "новая сессия с чистого листа"),
    ("resume", "продолжить сессию Claude по id (форк)"),
    ("sessions", "сохранённые сессии Claude"),
    ("history", "последние задачи"),
    ("id", "показать твой Telegram id"),
    ("ping", "проверка связи"),
    ("start", "справка"),
]

START_TEXT = (
    "tgbridge на связи.\n"
    "Пришли текст — он уйдёт в WSL как промпт для Claude Code.\n"
    "Каждый следующий промпт продолжает тот же разговор.\n"
    "/new — новая сессия, но контекст сохранить (форк текущей)\n"
    "/clear — новая сессия с чистого листа (забыть контекст)\n"
    "/resume <id> — продолжить конкретную сессию Claude (в новой ветке)\n"
    "/sessions — сохранённые сессии Claude (id для /resume)\n"
    "/history — последние задачи\n"
    "/id — показать твой Telegram id\n"
    "/ping — проверка"
)

HISTORY_LIMIT = 15
SESSIONS_LIMIT = 15
_PREVIEW = 60


def bot_commands() -> list[BotCommand]:
    """Список для меню-кнопки бота в Telegram."""
    return [BotCommand(command=c, description=d) for c, d in COMMANDS]


MD_CHUNK_LIMIT = 3900  # < 4096 (лимит Telegram), с запасом на служебные символы


def render_md_chunks(text: str, limit: int = MD_CHUNK_LIMIT) -> list[str]:
    """GFM-текст Claude -> список готовых MarkdownV2-кусков под лимит Telegram.

    Пустой вход -> []. Режем по границам строк/блоков (код-фенсы не рвём).
    Если конвертация упала — грубое экранирование обрезанного текста, лишь бы дошло.
    """
    text = text.strip()
    if not text:
        return []
    try:
        plain, entities = telegramify_markdown.convert(text)
        chunks = telegramify_markdown.split_markdownv2(plain, entities, max_utf16_len=limit)
        chunks = [c for c in chunks if c.strip()]
        if chunks:
            return chunks
    except Exception:  # noqa: BLE001 — форматирование не должно ронять доставку
        log.warning("не удалось отрендерить MarkdownV2, шлю как есть", exc_info=True)
    try:
        return [telegramify_markdown.markdownify(text[:limit])]
    except Exception:  # noqa: BLE001
        return [_escape_markdownv2(text[:limit])]


_MD_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!") | {"\\"}


def _escape_markdownv2(s: str) -> str:
    return "".join("\\" + ch if ch in _MD_V2_SPECIAL else ch for ch in s)


RESUME_CB_PREFIX = "resume:"


def resume_keyboard(session_id: str) -> InlineKeyboardMarkup | None:
    """Кнопка под результатом задачи: следующий промпт продолжит эту сессию Claude."""
    if not session_id:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Перейти к сессии",
                    callback_data=f"{RESUME_CB_PREFIX}{session_id}",
                )
            ]
        ]
    )


def id_reply(user_id: int, allowed_ids: set[int]) -> str:
    mark = "да" if user_id in allowed_ids else "НЕТ — добавь в TGBRIDGE_ALLOWED_USER_IDS"
    return f"твой id: `{user_id}`\nв allowlist: {mark}"


def clear_reply(user_id: int, allowed_ids: set[int], db: DB) -> str:
    """/clear: следующий промпт стартует новую сессию Claude с чистого листа."""
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    db.request_new_session()
    return "🧹 контекст очищен — следующий промпт начнёт новую сессию с нуля"


def new_reply(user_id: int, allowed_ids: set[int], db: DB) -> str:
    """/new: новая сессия Claude, но с сохранением контекста — форк текущей.

    Следующий промпт уедет как `--resume <последняя-сессия> --fork-session`.
    Если журнал сессий ещё пуст (форкать нечего) — обычный чистый старт.
    """
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    sid = db.latest_session_id()
    if not sid:
        db.request_new_session()
        return "🌱 сессий пока нет — следующий промпт начнёт новую с нуля"
    db.request_resume(sid)
    return (
        f"🌱 следующий промпт продолжит текущую сессию `{sid}` в новой ветке "
        "(контекст сохранён, исходная сессия не тронута)"
    )


def resume_reply(user_id: int, allowed_ids: set[int], db: DB, session_id: str) -> str:
    """/resume <id>: следующий промпт продолжит сессию <id> через --resume --fork-session."""
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    sid = (session_id or "").strip()
    if not sid or " " in sid:
        return "укажи id сессии: `/resume <session-id>`"
    db.request_resume(sid)
    return (
        f"🔗 следующий промпт продолжит сессию `{sid}` в новой ветке "
        "(исходная сессия не тронута)"
    )


def _clip_preview(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= _PREVIEW else one_line[:_PREVIEW] + "…"


def sessions_reply(
    user_id: int, allowed_ids: set[int], db: DB, limit: int = SESSIONS_LIMIT
) -> str:
    """Список сессий Claude, прошедших через мост, — id можно скопировать в /resume."""
    if user_id not in allowed_ids:
        return "не в allowlist, игнорирую"
    rows = db.sessions(limit)
    if not rows:
        return "нет сохранённых сессий"
    lines: list[str] = []
    for r in rows:
        when = time.strftime("%d.%m %H:%M", time.localtime(r["updated_at"]))
        lines.append(f"`{r['session_id']}` · {r['turns']} реплик · {when}")
        if r["title"]:
            lines.append(f"  {_clip_preview(r['title'])}")
        if r["last_result"]:
            lines.append(f"  ← {_clip_preview(r['last_result'])}")
    return "\n".join(lines)


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

    @dp.message(Command("clear"))
    async def on_clear(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(clear_reply(uid, allowed_ids, db))

    @dp.message(Command("new"))
    async def on_new(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(new_reply(uid, allowed_ids, db), parse_mode="Markdown")

    @dp.message(Command("resume"))
    async def on_resume(msg: Message, command: CommandObject, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(
            resume_reply(uid, allowed_ids, db, command.args or ""), parse_mode="Markdown"
        )

    @dp.message(Command("sessions"))
    async def on_sessions(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(sessions_reply(uid, allowed_ids, db), parse_mode="Markdown")

    @dp.message(Command("history"))
    async def on_history(msg: Message, db: DB) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(history_reply(uid, allowed_ids, db))

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(msg: Message, db: DB, wake: asyncio.Event) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(prompt_reply(msg.text, uid, msg.chat.id, allowed_ids, db, wake))

    @dp.callback_query(F.data.startswith(RESUME_CB_PREFIX))
    async def on_resume_cb(cb: CallbackQuery, db: DB) -> None:
        uid = cb.from_user.id if cb.from_user else 0
        sid = (cb.data or "")[len(RESUME_CB_PREFIX):]
        await cb.answer(resume_reply(uid, allowed_ids, db, sid)[:200])

    return dp


async def run_bot(bot: Bot, dp: Dispatcher, db: DB, wake: asyncio.Event) -> None:
    log.info("старт polling Telegram")
    try:
        await bot.set_my_commands(bot_commands())
    except Exception as e:  # noqa: BLE001 — меню-кнопка не критична для старта
        log.warning("не удалось выставить меню команд: %s", e)
    await dp.start_polling(bot, db=db, wake=wake, handle_signals=False)
