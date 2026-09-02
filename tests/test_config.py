from __future__ import annotations

import pytest

from tgbridge.config import Settings


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", set()),
        ("466404679", {466404679}),
        ("1,2,3", {1, 2, 3}),
        (" 1 , 2 ,3 ", {1, 2, 3}),
        ("7,7,7", {7}),
        ("10,", {10}),
    ],
)
def test_allowed_ids_parsing(raw, expected):
    assert Settings(allowed_user_ids=raw).allowed_ids == expected


def test_defaults_are_safe():
    s = Settings()
    assert s.token == "change-me"
    assert s.bot_token == ""
    assert s.allowed_ids == set()
    assert s.port == 8080
    assert s.prompt_timeout == 300
    assert s.projects_root == ""


def test_prompt_timeout_floor():
    with pytest.raises(ValueError):
        Settings(prompt_timeout=5)


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("TGBRIDGE_PORT", "9999")
    monkeypatch.setenv("TGBRIDGE_TOKEN", "from-env")
    s = Settings()
    assert s.port == 9999
    assert s.token == "from-env"
