"""Privacy helpers for the public viewer.

Hashes sensitive identifiers (IP, UA) with a rotating server-side pepper
so the raw values never hit the database. Pepper is rotated quarterly —
severing the link to a true IP across rotations.

Used by views + comments (Phase 3, 5). For v1 the pepper is in env
(IP_HASH_PEPPER); the rotate cadence is operational, not yet automated.
"""
from __future__ import annotations

import hashlib

from broadcaster.settings import get_settings


def hash_ip(ip: str) -> str:
    pepper = get_settings().ip_hash_pepper
    return hashlib.sha256(f"{ip}|{pepper}".encode()).hexdigest()


def hash_ua(ua: str) -> str:
    pepper = get_settings().ip_hash_pepper
    return hashlib.sha256(f"{ua}|{pepper}".encode()).hexdigest()
