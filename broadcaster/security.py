"""Password hashing + session cookie helpers.

Passwords are hashed with bcrypt (cost factor 12).
Sessions are signed cookies via starlette's SessionMiddleware.
"""
from __future__ import annotations

import secrets
import string

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


# Random-password alphabet for password-reset flows. Strips ambiguous
# characters (0/O, 1/l/I) so the temp password is easier to read out
# over a phone call.
_PASSWORD_ALPHABET = "".join(
    c for c in (string.ascii_letters + string.digits)
    if c not in "0O1lI"
)


def generate_strong_password(length: int = 14) -> str:
    """Return a cryptographically random password.

    Default length is 14 (≈ 83 bits of entropy with the 56-char
    alphabet — well above bcrypt's hashing effort). Used by the
    forgot-password flow to mint a one-shot temp password that the
    recovery operator relays to the requesting admin out-of-band.
    """
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
