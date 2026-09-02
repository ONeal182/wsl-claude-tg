from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from tgbridge.agent import main
from tgbridge.models import CommandOut


def _envelope(result: str = "", session_id: str = "s-1", is_error: bool = False) -> bytes:
    """Как `claude -p --output-format json` печатает итог."""
    return json.dumps(
        {"result": result, "session_id": session_id, "is_error": is_error}
    ).encode()


class FakeProc:
    def __init__(self, out=b"", rc=0, err=b""):
        self._out = out
        self._err = err
        self.returncode = rc
        self.killed = False

    async def communicate(self):
        return self._out, self._err

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


# --- scan_projects ---------------------------------------------------


def test_scan_projects_returns_non_hidden_dirs(tmp_path):
    (tmp_path / "blog").mkdir()
    (tmp_path / "tgbridge").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    assert main.scan_projects(str(tmp_path)) == [
        ("blog", str(tmp_path / "blog")),
        ("tgbridge", str(tmp_path / "tgbridge")),
    ]


def test_scan_projects_missing_root(tmp_path):
    assert main.scan_projects(str(tmp_path / "nope")) == []


def test_scan_projects_empty_root():
    assert main.scan_projects("") == []


# --- run_prompt --------------------------------------------------------


async def test_run_prompt_parses_result_and_session_id(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(_envelope("hello", "sess-42"), 0))
    assert await main.run_prompt(settings, "hi") == (True, "hello", "sess-42")


async def test_run_prompt_json_is_error_is_failure(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(_envelope("boom", "sess-1", is_error=True), 0))
    ok, out, sid = await main.run_prompt(settings, "hi")
    assert ok is False and out == "boom" and sid == "sess-1"


async def test_run_prompt_nonzero_rc(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(_envelope("boom", "s"), 2))
    ok, out, _ = await main.run_prompt(settings, "hi")
    assert ok is False and out == "boom"


async def test_run_prompt_non_json_falls_back_to_raw(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(b"segfault", 1, err=b"stack trace"))
    ok, out, sid = await main.run_prompt(settings, "hi")
    assert ok is False and "segfault" in out and sid == ""


async def test_run_prompt_empty_output(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(_envelope("   ", "s"), 0))
    assert await main.run_prompt(settings, "hi") == (True, "(пустой вывод)", "s")


async def test_run_prompt_truncates(settings, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(_envelope("x" * 9000, "s"), 0))
    ok, out, _ = await main.run_prompt(settings, "hi")
    assert ok is True and len(out) == main.MAX_OUTPUT


async def test_run_prompt_binary_missing(settings, monkeypatch):
    async def _boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _boom)
    ok, out, sid = await main.run_prompt(settings, "hi")
    assert ok is False and "не найден бинарь claude" in out and sid == ""


async def test_run_prompt_asks_for_json_output(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi")
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[-1] == "hi"


async def test_run_prompt_fresh_starts_new_session(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi", fresh=True)
    assert "--continue" not in argv and "-c" not in argv
    assert argv[-1] == "hi"


async def test_run_prompt_not_fresh_continues_session(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi", fresh=False)
    assert "--continue" in argv
    assert argv[-1] == "hi"


async def test_run_prompt_resume_forks_session(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi", resume_from="sess-abc")
    assert argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--fork-session" in argv and "--continue" not in argv
    assert argv[-1] == "hi"


async def test_run_prompt_resume_beats_fresh(settings, monkeypatch):
    argv = _capture_argv(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi", fresh=True, resume_from="sess-abc")
    assert "--resume" in argv and "--fork-session" in argv


def _capture_kw(monkeypatch, proc):
    seen: dict = {}

    async def _fake(*argv, **kw):
        seen.update(kw)
        return proc

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", _fake)
    return seen


async def test_run_prompt_runs_in_cmd_cwd(settings, monkeypatch):
    kw = _capture_kw(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi", cwd="/home/oneal/proj")
    assert kw["cwd"] == "/home/oneal/proj"


async def test_run_prompt_defaults_cwd_to_workdir(settings, monkeypatch):
    kw = _capture_kw(monkeypatch, FakeProc(_envelope("ok", "s"), 0))
    await main.run_prompt(settings, "hi")
    assert kw["cwd"] == settings.workdir


async def test_run_prompt_timeout_kills(settings, monkeypatch):
    proc = FakeProc(b"", 0)
    _patch_exec(monkeypatch, proc)

    async def _timeout(coro, timeout):
        coro.close()  # не оставлять «never awaited»
        raise TimeoutError

    monkeypatch.setattr(main.asyncio, "wait_for", _timeout)
    ok, out, sid = await main.run_prompt(settings, "hi")
    assert ok is False and out.startswith("таймаут") and proc.killed and sid == ""


# --- handle ----------------------------------------------------------


async def test_handle_posts_result(settings, monkeypatch):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(True, "out", "sess-9")))
    client = AsyncMock()
    client.post.return_value.raise_for_status = lambda: None
    await main.handle(client, settings, CommandOut(id=7, prompt="p", chat_id=1))
    client.post.assert_awaited_once()
    url, kw = client.post.await_args.args[0], client.post.await_args.kwargs
    assert url == "/commands/7/result" and kw["json"]["ok"] is True
    assert kw["json"]["session_id"] == "sess-9"


async def test_handle_forwards_session_flags_to_run_prompt(settings, monkeypatch):
    rp = AsyncMock(return_value=(True, "out", ""))
    monkeypatch.setattr(main, "run_prompt", rp)
    client = AsyncMock()
    client.post.return_value.raise_for_status = lambda: None
    cmd = CommandOut(id=7, prompt="p", chat_id=1, fresh=True, resume_from="sess-abc", cwd="/x")
    await main.handle(client, settings, cmd)
    assert rp.await_args.kwargs.get("fresh") is True
    assert rp.await_args.kwargs.get("resume_from") == "sess-abc"
    assert rp.await_args.kwargs.get("cwd") == "/x"


async def test_handle_retries_then_succeeds(settings, monkeypatch):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(True, "out", "")))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    ok_resp = AsyncMock()
    ok_resp.raise_for_status = lambda: None
    client = AsyncMock()
    client.post.side_effect = [httpx.ConnectError("x"), httpx.ConnectError("x"), ok_resp]
    await main.handle(client, settings, CommandOut(id=1, prompt="p", chat_id=1))
    assert client.post.await_count == 3


async def test_handle_gives_up_after_5(settings, monkeypatch, caplog):
    monkeypatch.setattr(main, "run_prompt", AsyncMock(return_value=(False, "err", "")))
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


async def test_push_projects_posts_scanned_list(settings, monkeypatch, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    monkeypatch.setattr(settings, "projects_root", str(tmp_path))
    client = AsyncMock()
    client.post.return_value.raise_for_status = lambda: None

    await main.push_projects(client, settings)

    url, kw = client.post.await_args.args[0], client.post.await_args.kwargs
    assert url == "/projects"
    names = [p["name"] for p in kw["json"]["projects"]]
    assert names == ["alpha", "beta"]


async def test_push_projects_noop_when_root_unset(settings, monkeypatch):
    monkeypatch.setattr(settings, "projects_root", "")
    client = AsyncMock()
    await main.push_projects(client, settings)
    client.post.assert_not_awaited()


async def test_push_projects_swallows_http_error(settings, monkeypatch, tmp_path):
    (tmp_path / "alpha").mkdir()
    monkeypatch.setattr(settings, "projects_root", str(tmp_path))
    client = AsyncMock()
    client.post.side_effect = httpx.ConnectError("down")
    await main.push_projects(client, settings)  # не должно бросить


async def test_loop_syncs_projects_before_polling(settings, monkeypatch):
    fake = FakeClient([asyncio.CancelledError()])
    _install_client(monkeypatch, fake)
    pushed = AsyncMock()
    monkeypatch.setattr(main, "push_projects", pushed)
    with pytest.raises(asyncio.CancelledError):
        await main.loop(settings)
    pushed.assert_awaited_once()


async def test_loop_backoff_on_http_error(settings, monkeypatch):
    fake = FakeClient([httpx.ConnectError("x"), asyncio.CancelledError()])
    _install_client(monkeypatch, fake)
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main.loop(settings)
    sleep.assert_awaited_with(1)
