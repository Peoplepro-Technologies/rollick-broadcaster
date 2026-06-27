"""Settings K/V store (non-secret prefs only).

Secrets (SMTP_*, WHATSAPP_*, SESSION_SECRET, IP_HASH_PEPPER) live in env
only. The settings table is for runtime knobs the admin can change
without restarting: app_brand_name, base_public_url, link_token_ttl_days,
anti-spam thresholds.
"""
from __future__ import annotations

from broadcaster.db import get_db


def get(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_visible() -> dict[str, str]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
