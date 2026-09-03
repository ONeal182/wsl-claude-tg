from __future__ import annotations

import io

import httpx
import pytest

from tgbridge.cli import notify, rcsync


class FakeResp:
    def raise_for_status(self):
        pass


@pytest.fixture
def captured_post(monkeypatch):
    calls = []

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return FakeResp()

    monkeypatch.setattr(notify.httpx, "post", _post)
    return calls


def _run(monkeypatch, argv, stdin=""):
    monkeypatch.setattr("sys.argv", ["tgnotify", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return notify.main()


def test_sends_text_default_info(monkeypatch, captured_post):
    assert _run(monkeypatch, ["сборка готова"]) == 0
    call = captured_post[0]
    assert call["url"].endswith("/notify")
    assert call["json"] == {"text": "сборка готова", "level": "info"}
    assert call["headers"]["Authorization"].startswith("Bearer ")


def test_level_flag(monkeypatch, captured_post):
    assert _run(monkeypatch, ["-l", "error", "тесты упали"]) == 0
    assert captured_post[0]["json"]["level"] == "error"


def test_session_flag_adds_session_id(monkeypatch, captured_post):
    assert _run(monkeypatch, ["--session", "sess-abc", "готово"]) == 0
    assert captured_post[0]["json"]["session_id"] == "sess-abc"


def test_no_session_flag_omits_session_id(monkeypatch, captured_post):
    assert _run(monkeypatch, ["готово"]) == 0
    assert "session_id" not in captured_post[0]["json"]


def test_reads_stdin_on_dash(monkeypatch, captured_post):
    assert _run(monkeypatch, ["-"], stdin="из пайпа\n") == 0
    assert captured_post[0]["json"]["text"] == "из пайпа"


def test_empty_text_exit_2(monkeypatch, captured_post):
    assert _run(monkeypatch, ["-"], stdin="   \n") == 2
    assert captured_post == []


def test_network_error_exit_1(monkeypatch):
    def _boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(notify.httpx, "post", _boom)
    assert _run(monkeypatch, ["привет"]) == 1


# --- tgbridge-rcsync ------------------------------------------------------


def test_env_url_from_journal_picks_last():
    text = (
        "старт\n"
        "https://claude.ai/code?environment=env_OLD1\n"
        "переподключение\n"
        "https://claude.ai/code?environment=env_NEW2\n"
    )
    assert rcsync.env_url_from_journal(text) == "https://claude.ai/code?environment=env_NEW2"


def test_env_url_from_journal_absent():
    assert rcsync.env_url_from_journal("ничего интересного") == ""


def test_instances_from_units_parses_and_dedups():
    text = (
        "  claude-rc@monorepo.service loaded active running ...\n"
        "  claude-rc@tgbridge.service loaded active running ...\n"
        "  claude-rc@monorepo.service loaded active running ...\n"
        "  tgbridge-tunnel.service    loaded active running ...\n"
    )
    assert rcsync.instances_from_units(text) == ["monorepo", "tgbridge"]


def test_rcsync_main_posts_collected_projects(monkeypatch, captured_post):
    monkeypatch.setattr(
        rcsync, "collect",
        lambda home: [{"name": "monorepo", "path": "/home/oneal/monorepo", "env_url": "env-A"}],
    )
    assert rcsync.main() == 0
    call = captured_post[0]
    assert call["url"].endswith("/projects")
    assert call["json"] == {
        "projects": [{"name": "monorepo", "path": "/home/oneal/monorepo", "env_url": "env-A"}]
    }
    assert call["headers"]["Authorization"].startswith("Bearer ")


def test_rcsync_main_no_projects_skips_post(monkeypatch, captured_post):
    monkeypatch.setattr(rcsync, "collect", lambda home: [])
    assert rcsync.main() == 0
    assert captured_post == []


def test_rcsync_main_network_error_exit_1(monkeypatch):
    monkeypatch.setattr(
        rcsync, "collect", lambda home: [{"name": "x", "path": "/x", "env_url": ""}]
    )

    def _boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(rcsync.httpx, "post", _boom)
    assert rcsync.main() == 1
