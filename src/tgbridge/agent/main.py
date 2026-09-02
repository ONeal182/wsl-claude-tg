"""Агент в WSL: тянет задачи с VPS через long-poll и выполняет их в Claude Code.

Только исходящие соединения — домашней машине не нужен ни белый ip, ни проброс
портов. При обрыве сети / засыпании ПК просто повторяет попытку с backoff.

Цикл:
    GET  {server}/commands/next?timeout=25   -> задача или 204
    claude -p --output-format json "<prompt>" -> {result, session_id, is_error}
    POST {server}/commands/{id}/result       -> {ok, output, session_id}
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ..config import Settings, load
from ..models import CommandOut, ResultIn

log = logging.getLogger("tgbridge.agent")

MAX_OUTPUT = 8000  # больше в Telegram всё равно не уедет


def _parse_output(raw_out: bytes, raw_err: bytes, rc: int) -> tuple[bool, str, str]:
    """Разобрать вывод `claude -p --output-format json`.

    Успешный разбор -> (ok, .result, .session_id). Если json не распознан
    (краш до печати конверта) -> (rc == 0, сырой текст, "").
    """
    try:
        data = json.loads(raw_out or b"{}")
    except (json.JSONDecodeError, ValueError):
        text = (raw_out + b"\n" + raw_err).decode("utf-8", "replace").strip()
        return rc == 0, (text or "(пустой вывод)")[:MAX_OUTPUT], ""
    out = str(data.get("result") or "").strip()[:MAX_OUTPUT]
    session_id = str(data.get("session_id") or "")
    ok = rc == 0 and not data.get("is_error", False)
    return ok, out or "(пустой вывод)", session_id


async def run_prompt(
    cfg: Settings, prompt: str, fresh: bool = False, resume_from: str = ""
) -> tuple[bool, str, str]:
    """Выполнить промпт через `claude -p` в headless-режиме. Вернуть (ok, вывод, session_id).

    `resume_from` (непусто) — `--resume <id> --fork-session`: продолжить конкретную
        сессию, но в новой ветке (исходная не затрагивается). Перевешивает `fresh`.
    `fresh=True`  — начать новую сессию Claude (после /clear, /new или самый первый промпт).
    `fresh=False` — `--continue`: подхватить контекст предыдущего разговора в workdir.
    """
    argv = [cfg.claude_bin, "-p", "--output-format", "json"]
    if resume_from:
        argv += ["--resume", resume_from, "--fork-session"]
    elif not fresh:
        argv.append("--continue")
    argv.append(prompt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cfg.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, f"не найден бинарь claude: {cfg.claude_bin!r}", ""

    try:
        raw_out, raw_err = await asyncio.wait_for(
            proc.communicate(), timeout=cfg.prompt_timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"таймаут {cfg.prompt_timeout}s", ""

    return _parse_output(raw_out, raw_err, proc.returncode or 0)


async def handle(client: httpx.AsyncClient, cfg: Settings, cmd: CommandOut) -> None:
    log.info(
        "задача #%s (fresh=%s resume_from=%s): %s",
        cmd.id, cmd.fresh, cmd.resume_from or "-", cmd.prompt[:80],
    )
    ok, output, session_id = await run_prompt(
        cfg, cmd.prompt, fresh=cmd.fresh, resume_from=cmd.resume_from
    )
    result = ResultIn(ok=ok, output=output, session_id=session_id)
    for attempt in range(5):
        try:
            r = await client.post(f"/commands/{cmd.id}/result", json=result.model_dump())
            r.raise_for_status()
            log.info("задача #%s закрыта (ok=%s)", cmd.id, ok)
            return
        except httpx.HTTPError as e:
            log.warning("не отдал результат #%s (попытка %d): %s", cmd.id, attempt + 1, e)
            await asyncio.sleep(2 ** attempt)
    log.error("результат #%s потерян", cmd.id)


async def loop(cfg: Settings) -> None:
    headers = {"Authorization": f"Bearer {cfg.token}"}
    backoff = 1
    async with httpx.AsyncClient(base_url=cfg.server_url, headers=headers, timeout=40) as client:
        log.info("агент запущен, сервер %s", cfg.server_url)
        while True:
            try:
                r = await client.get("/commands/next", params={"timeout": 25})
                if r.status_code == 204:
                    backoff = 1
                    continue
                r.raise_for_status()
                cmd = CommandOut.model_validate(r.json())
                backoff = 1
                await handle(client, cfg, cmd)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("нет связи с сервером: %s (жду %ds)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(loop(load()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
