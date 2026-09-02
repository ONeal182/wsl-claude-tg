"""Проверка общего секрета в заголовке Authorization: Bearer <token>."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status


def make_auth_dep(expected: str):
    def check(authorization: str = Header(default="")) -> None:
        prefix = "Bearer "
        got = authorization[len(prefix):] if authorization.startswith(prefix) else ""
        # сравнение с защитой от тайминг-атаки
        if not got or not secrets.compare_digest(got, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")

    return check
