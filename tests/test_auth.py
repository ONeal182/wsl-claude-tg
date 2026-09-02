from __future__ import annotations

import pytest
from fastapi import HTTPException

from tgbridge.server.auth import make_auth_dep

check = None


def setup_function():
    global check
    check = make_auth_dep("s3cret")


def test_correct_token_passes():
    assert check("Bearer s3cret") is None


@pytest.mark.parametrize(
    "header",
    [
        "",
        "s3cret",              # без схемы
        "Bearer wrong",
        "Bearer ",
        "Basic s3cret",
        "bearer s3cret",       # регистр схемы важен
        "Bearer s3cret extra",
    ],
)
def test_bad_headers_rejected(header):
    with pytest.raises(HTTPException) as ei:
        check(header)
    assert ei.value.status_code == 401
