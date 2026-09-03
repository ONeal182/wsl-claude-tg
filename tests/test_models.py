from __future__ import annotations

import pytest
from pydantic import ValidationError

from tgbridge.models import CommandOut, NotifyIn, ProjectItem, ProjectsIn, ResultIn


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


def test_notify_defaults_empty_session_id():
    assert NotifyIn(text="hi").session_id == ""


def test_notify_carries_session_id():
    n = NotifyIn(text="hi", session_id="b463918f-9e3d")
    assert n.model_dump()["session_id"] == "b463918f-9e3d"


def test_result_defaults_empty_output():
    r = ResultIn(ok=True)
    assert r.output == "" and r.session_id == ""


def test_result_carries_session_id():
    r = ResultIn(ok=True, output="done", session_id="b463918f-9e3d")
    assert r.model_dump()["session_id"] == "b463918f-9e3d"


def test_command_out_defaults():
    c = CommandOut(id=1, prompt="p", chat_id=2)
    assert c.fresh is False and c.resume_from == "" and c.cwd == ""


def test_command_out_roundtrip():
    c = CommandOut(
        id=3, prompt="p", chat_id=42, fresh=True, resume_from="sess-abc", cwd="/home/oneal/x"
    )
    assert c.model_dump() == {
        "id": 3,
        "prompt": "p",
        "chat_id": 42,
        "fresh": True,
        "resume_from": "sess-abc",
        "cwd": "/home/oneal/x",
    }


def test_projects_in_defaults_empty_list():
    assert ProjectsIn().projects == []


def test_projects_in_parses_items():
    p = ProjectsIn(projects=[{"name": "tgbridge", "path": "/home/oneal/tgbridge"}])
    assert p.projects[0] == ProjectItem(name="tgbridge", path="/home/oneal/tgbridge")


def test_project_item_rejects_empty_fields():
    with pytest.raises(ValidationError):
        ProjectItem(name="", path="/x")
    with pytest.raises(ValidationError):
        ProjectItem(name="x", path="")


def test_project_item_env_url_defaults_empty():
    assert ProjectItem(name="x", path="/x").env_url == ""


def test_project_item_carries_env_url():
    p = ProjectItem(name="x", path="/x", env_url="https://claude.ai/code?environment=env_1")
    assert p.model_dump()["env_url"] == "https://claude.ai/code?environment=env_1"
