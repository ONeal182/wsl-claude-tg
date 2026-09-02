"""Telegram-бот на aiogram: long-poll к Telegram, приём промптов, allowlist.

Живёт в одном процессе с FastAPI (запускается как фоновая задача в lifespan).
Любое текстовое сообщение от разрешённого пользователя кладётся в очередь как задача.

Логика ответов вынесена в чистые функции (`*_reply`) — их и покрываем тестами;
хендлеры aiogram остаются тонкими обёртками.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from ..db import DB

log = logging.getLogger("tgbridge.bot")

START_TEXT = (
    "tgbridge на связи.\n"
    "Пришли текст — он уйдёт в WSL как промпт для Claude Code.\n"
    "/id — показать твой Telegram id\n"
    "/ping — проверка"
)


def id_reply(user_id: int, allowed_ids: set[int]) -> str:
    mark = "да" if user_id in allowed_ids else "НЕТ — добавь в TGBRIDGE_ALLOWED_USER_IDS"
    return f"твой id: `{user_id}`\nв allowlist: {mark}"


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

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(msg: Message, db: DB, wake: asyncio.Event) -> None:
        uid = msg.from_user.id if msg.from_user else 0
        await msg.answer(prompt_reply(msg.text, uid, msg.chat.id, allowed_ids, db, wake))

    return dp


async def run_bot(bot: Bot, dp: Dispatcher, db: DB, wake: asyncio.Event) -> None:
    log.info("старт polling Telegram")
    await dp.start_polling(bot, db=db, wake=wake, handle_signals=False)
