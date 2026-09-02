from __future__ import annotations

import pytest
from pydantic import ValidationError

from tgbridge.models import CommandOut, NotifyIn, ResultIn


def test_notify_defaults_level_info():
    assert NotifyIn(text="hi").level == "info"


def test_notify_rejects_empty_text():
    with pytest.raises(ValidationError):
        NotifyIn(text="")


def test_notify_rejects_overlong_text():
    with pytest.raises(ValidationError):
        NotifyIn(text="x" * 4001)


def test_notify_rejects_bad_level():
    with pytest.raises(ValidationError):
        NotifyIn(text="hi", level="debug")


def test_result_defaults_empty_output():
    r = ResultIn(ok=True)
    assert r.output == ""


def test_command_out_defaults_fresh_false():
    assert CommandOut(id=1, prompt="p", chat_id=2).fresh is False


def test_command_out_roundtrip():
    c = CommandOut(id=3, prompt="p", chat_id=42, fresh=True)
    assert c.model_dump() == {"id": 3, "prompt": "p", "chat_id": 42, "fresh": True}
