"""FastAPI-приложение на VPS: HTTP API для WSL + фоновый Telegram-бот.

Эндпоинты (все, кроме /healthz, требуют Authorization: Bearer <TGBRIDGE_TOKEN>):
    GET  /healthz                  — проверка живости, без авторизации
    POST /notify                   — {text, level} -> сообщение во все чаты allowlist
    GET  /commands/next?timeout=25 — long-poll: 200 с задачей или 204, если пусто
    POST /commands/{id}/result     — {ok, output} -> ответ в Telegram, задача закрыта
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from aiogram import Bot
from fastapi import Depends, FastAPI, Response
from fastapi.responses import JSONResponse

from ..config import Settings, load
from ..db import DB
from ..models import NotifyIn, ResultIn
from .auth import make_auth_dep
from .bot import build_dispatcher, resume_keyboard, run_bot

log = logging.getLogger("tgbridge.server")

MAX_TG_LEN = 3800  # запас под лимит Telegram в 4096


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TG_LEN else text[:MAX_TG_LEN] + "\n…(обрезано)"


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = DB(cfg.db_path)
        wake = asyncio.Event()
        bot = Bot(cfg.bot_token) if cfg.bot_token else None
        task: asyncio.Task | None = None
        if bot is not None:
            dp = build_dispatcher(cfg.allowed_ids)
            task = asyncio.create_task(run_bot(bot, dp, db, wake))
        else:
            log.warning("TGBRIDGE_BOT_TOKEN пуст — бот не запущен, работает только HTTP API")

        app.state.db = db
        app.state.wake = wake
        app.state.bot = bot
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if bot is not None:
                await bot.session.close()
            db.close()

    app = FastAPI(title="tgbridge", version="0.1.0", lifespan=lifespan)
    auth = Depends(make_auth_dep(cfg.token))

    async def notify_chats(bot: Bot | None, text: str) -> None:
        if bot is None:
            return
        for chat_id in cfg.allowed_ids:
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id, text)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/notify", dependencies=[auth])
    async def notify(body: NotifyIn) -> dict[str, bool]:
        db: DB = app.state.db
        db.log_notification(body.text, body.level)
        mark = {"info": "ℹ️", "warn": "⚠️", "error": "🔴"}[body.level]
        await notify_chats(app.state.bot, f"{mark} {body.text}")
        return {"ok": True}

    @app.get("/commands/next", dependencies=[auth])
    async def commands_next(timeout: float = 25.0) -> Response:
        db: DB = app.state.db
        wake: asyncio.Event = app.state.wake
        deadline = time.monotonic() + max(1.0, min(timeout, 60.0))
        while True:
            cmd = db.lease_next()
            if cmd is not None:
                return JSONResponse(cmd.model_dump())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Response(status_code=204)
            wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=min(remaining, 5.0))

    @app.post("/commands/{command_id}/result", dependencies=[auth])
    async def commands_result(command_id: int, body: ResultIn) -> dict[str, bool]:
        db: DB = app.state.db
        chat_id = db.finish(command_id, body.ok, body.output, body.session_id)
        if chat_id is not None and app.state.bot is not None:
            head = f"✅ #{command_id}" if body.ok else f"❌ #{command_id}"
            text = _clip(f"{head}\n\n{body.output}" if body.output else head)
            if body.session_id:
                text += f"\n\n🧩 сессия {body.session_id}"
            with contextlib.suppress(Exception):
                await app.state.bot.send_message(
                    chat_id, text, reply_markup=resume_keyboard(body.session_id)
                )
        return {"ok": chat_id is not None}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
