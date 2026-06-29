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


def runtime_overrides() -> dict[str, str]:
    """Return the effective SMTP + WhatsApp config the app is currently
    using. Backed by `Settings` (which already merges DB-stored overrides
    on top of .env defaults via `get_settings()`).

    User-supplied credentials (smtp_pass, whatsapp_access_token,
    whatsapp_app_secret) ARE included intentionally — admins edit them in
    place from the settings page. Server-internal secrets
    (session_secret, ip_hash_pepper, media_sign_secret) are NOT here; those
    stay in `.env`.
    """
    from broadcaster.settings import get_settings
    s = get_settings()
    return {
        "smtp_host": s.smtp_host,
        "smtp_port": s.smtp_port,
        "smtp_user": s.smtp_user,
        "smtp_from": s.smtp_from,
        "smtp_pass": s.smtp_pass,
        "whatsapp_phone_id": s.whatsapp_phone_id,
        "whatsapp_api_version": s.whatsapp_api_version,
        "whatsapp_country_code": s.whatsapp_country_code,
        "whatsapp_access_token": s.whatsapp_access_token,
        "whatsapp_app_secret": s.whatsapp_app_secret,
    }
