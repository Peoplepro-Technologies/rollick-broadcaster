"""Password hashing + session cookie helpers.

Passwords are hashed with bcrypt (cost factor 12).
Sessions are signed cookies via starlette's SessionMiddleware.
"""
from __future__ import annotations

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)
