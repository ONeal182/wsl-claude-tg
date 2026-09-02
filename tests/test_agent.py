from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from tgbridge.agent import main
from tgbridge.models import CommandOut


class FakeProc:
    def __init__(self, out=b"", rc=0):
        self._out = out
        self.returncode = rc
        self.killed = False

    async def communicate(self):
        return self._out, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_exec(monkeypatch, proc):
    async def _fake(*a, **kw):
        return proc

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _fake)


def _capture_argv(monkeypatch, proc):
    seen: list[str] = []

    async def _fake(*argv, **kw):
        seen.extend(argv)
        return proc

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _fake)
    return seen


# --- run_prompt --------------------------------------------------------


async def test_run_prompt_success(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(b"hello", 0))
    assert await main.run_prompt(settings, "hi") == (True, "hello")


async def test_run_prompt_nonzero_rc(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(b"boom", 2))
    ok, out = await main.run_prompt(settings, "hi")
    assert ok is False and out == "boom"


async def test_run_prompt_empty_output(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(b"   ", 0))
    assert await main.run_prompt(settings, "hi") == (True, "(пустой вывод)")


async def test_run_prompt_truncates(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(b"x" * 9000, 0))
    ok, out = await main.run_prompt(settings, "hi")
    assert ok is True and len(out) == main.MAX_OUTPUT


async def test_run_prompt_binary_missing(settings, monkeypatch):
    async def _boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _boom)
    ok, out = await main.run_prompt(settings, "hi")
    assert ok is False and "не найден бинарь claude" in out


async def test_run_prompt_fresh_starts_new_session(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(b"ok", 0))
    await main.run_prompt(settings, "hi", fresh=True)
    assert "--continue" not in argv and "-c" not in argv
    assert argv[-1] == "hi"


async def test_run_prompt_not_fresh_continues_session(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(b"ok", 0))
    await main.run_prompt(settings, "hi", fresh=False)
    assert "--continue" in argv
    assert argv[-1] == "hi"


async def test_run_prompt_timeout_kills(settings, monkeypatch):
    proc = FakeProc(b"", 0)
    _patch_exec(monkeypatch, proc)

    async def _timeout(coro, timeout):
        coro.close()  # не оставлять «never awaited»
        raise TimeoutError

    monkeypatch.setattr(main.asyncio, "wait_for", _timeout)
    ok, out = await main.run_prompt(settings, "hi")
    assert ok is False and out.startswith("таймаут") and proc.killed


# --- handle ----------------------------------------------------------


async def test_handle_posts_result(settings, monkeypatch):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(True, "out")))
    client = AsyncMock()
    client.post.return_value.raise_for_status = lambda: None
    await main.handle(client, settings, CommandOut(id=7, prompt="p", chat_id=1))
    client.post.assert_awaited_once()
    url, kw = client.post.await_args.args[0], client.post.await_args.kwargs
    assert url == "/commands/7/result" and kw["json"]["ok"] is True


async def test_handle_forwards_fresh_flag_to_run_prompt(settings, monkeypatch):
    rp = AsyncMock(return_value=(True, "out"))
    monkeypatch.setattr(main, "run_prompt", rp)
    client = AsyncMock()
    client.post.return_value.raise_for_status = lambda: None
    await main.handle(client, settings, CommandOut(id=7, prompt="p", chat_id=1, fresh=True))
    assert rp.await_args.kwargs.get("fresh") is True or rp.await_args.args[2] is True


async def test_handle_retries_then_succeeds(settings, monkeypatch):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(True, "out")))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    ok_resp = AsyncMock()
    ok_resp.raise_for_status = lambda: None
    client = AsyncMock()
    client.post.side_effect = [httpx.ConnectError("x"), httpx.ConnectError("x"), ok_resp]
    await main.handle(client, settings, CommandOut(id=1, prompt="p", chat_id=1))
    assert client.post.await_count == 3


async def test_handle_gives_up_after_5(settings, monkeypatch, caplog):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(False, "err")))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    client = AsyncMock()
    client.post.side_effect = httpx.ConnectError("down")
    await main.handle(client, settings, CommandOut(id=9, prompt="p", chat_id=1))
    assert client.post.await_count == 5
    assert "потерян" in caplog.text


# --- loop (один проход) -------------------------------------------


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("e", request=None, response=None)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, get_results):
        self._get_results = list(get_results)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append(url)
        r = self._get_results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _install_client(monkeypatch, fake):
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **kw: fake)


async def test_loop_204_then_stops(settings, monkeypatch):
    fake = FakeClient([FakeResp(204), asyncio.CancelledError()])
    _install_client(monkeypatch, fake)
    with pytest.raises(asyncio.CancelledError):
        await main.loop(settings)
    assert fake.calls == ["/commands/next", "/commands/next"]


async def test_loop_dispatches_command(settings, monkeypatch):
    fake = FakeClient(
        [FakeResp(200, {"id": 3, "prompt": "do", "chat_id": 8}), asyncio.CancelledError()]
    )
    _install_client(monkeypatch, fake)
    handled = AsyncMock()
    monkeypatch.setattr(main, "handle", handled)
    with pytest.raises(asyncio.CancelledError):
        await main.loop(settings)
    handled.assert_awaited_once()
    assert handled.await_args.args[2] == CommandOut(id=3, prompt="do", chat_id=8)


async def test_loop_backoff_on_http_error(settings, monkeypatch):
    fake = FakeClient([httpx.ConnectError("x"), asyncio.CancelledError()])
    _install_client(monkeypatch, fake)
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main.loop(settings)
    sleep.assert_awaited_with(1)
