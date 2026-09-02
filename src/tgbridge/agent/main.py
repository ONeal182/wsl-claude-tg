"""Агент в WSL: тянет задачи с VPS через long-poll и выполняет их в Claude Code.

Только исходящие соединения — домашней машине не нужен ни белый ip, ни проброс
портов. При обрыве сети / засыпании ПК просто повторяет попытку с backoff.

Цикл:
    GET  {server}/commands/next?timeout=25   -> задача или 204
    claude -p "<prompt>"                     -> stdout
    POST {server}/commands/{id}/result       -> {ok, output}
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import Settings, load
from ..models import CommandOut, ResultIn

log = logging.getLogger("tgbridge.agent")

MAX_OUTPUT = 8000  # больше в Telegram всё равно не уедет


async def run_prompt(
    cfg: Settings, prompt: str, fresh: bool = False, resume_from: str = ""
) -> tuple[bool, str]:
    """Выполнить промпт через `claude -p` в headless-режиме.

    `resume_from` (непусто) — `--resume <id> --fork-session`: продолжить конкретную
        сессию, но в новой ветке (исходная не затрагивается). Перевешивает `fresh`.
    `fresh=True`  — начать новую сессию Claude (после /clear, /new или самый первый промпт).
    `fresh=False` — `--continue`: подхватить контекст предыдущего разговора в workdir.
    """
    argv = [cfg.claude_bin, "-p"]
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
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"не найден бинарь claude: {cfg.claude_bin!r}"

    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=cfg.prompt_timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"таймаут {cfg.prompt_timeout}s"

    out = raw.decode("utf-8", "replace").strip()[:MAX_OUTPUT]
    return proc.returncode == 0, out or "(пустой вывод)"


async def handle(client: httpx.AsyncClient, cfg: Settings, cmd: CommandOut) -> None:
    log.info(
        "задача #%s (fresh=%s resume_from=%s): %s",
        cmd.id, cmd.fresh, cmd.resume_from or "-", cmd.prompt[:80],
    )
    ok, output = await run_prompt(cfg, cmd.prompt, fresh=cmd.fresh, resume_from=cmd.resume_from)
    result = ResultIn(ok=ok, output=output)
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
