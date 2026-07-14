"""Application settings loaded from environment variables.

Secrets must come from env (never the settings DB table). Non-secret
preferences may be overridden via the settings DB table at runtime —
`get_settings()` returns env values merged with any DB-stored overrides.
Call `bust_settings_cache()` after writes so subsequent reads see the
new values.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "Rollick Broadcaster"
    app_base_url: str = "http://localhost:8123"
    base_public_url: str = "http://localhost:8123"
    link_token_ttl_days: int = 30

    # Admin bootstrap
    admin_username: str = "admin"
    admin_password: str = Field(default="change-me-now")

    # Session signing
    session_secret: str = Field(default="change-me-to-a-random-string-at-least-32-chars")

    # Database
    database_url: str = "broadcaster.db"

    # Anti-spam
    comment_max_per_ip_per_hour: int = 5
    comment_max_per_link_lifetime: int = 3
    comment_cooldown_seconds: int = 30

    # Anonymity
    ip_hash_pepper: str = Field(default="change-me-quarterly")

    # Media signing
    media_sign_secret: str = Field(default="change-me")

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""

    # WhatsApp Business API
    whatsapp_phone_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_country_code: str = "91"

    # AiSensy (preferred WhatsApp provider when configured). Only the
    # API key stays env-only; campaign_name + base_url can be overridden
    # at runtime from /admin/settings (stored in the settings DB table).
    aisensy_api_key: str = ""
    aisensy_campaign_name: str = ""
    aisensy_base_url: str = "https://backend.aisensy.com/campaign/t1/api/v2"


@lru_cache(maxsize=1)
def _env_settings() -> Settings:
    """Pure env-based settings. Cached so the disk read happens once."""
    return Settings()


@lru_cache(maxsize=1)
def _db_overrides() -> dict:
    """Cached snapshot of DB-stored settings. Invalidated by
    `bust_settings_cache()` after writes. Without this cache, every
    `get_settings()` call opens a fresh SQLite connection (≈70ms) just
    to read 0–4 rows from the `settings` table.

    Returns {} on any error — most commonly because the `settings`
    table doesn't exist yet (during `init_db()` itself, or before the
    schema migration runs). The caller falls back to env-only values
    in that case.
    """
    try:
        from broadcaster.services.settings import all_visible
        return all_visible()
    except Exception:
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Effective settings: env values overridden by DB-stored prefs.

    DB overrides apply to non-secret keys only. Secrets (smtp_pass,
    whatsapp_access_token, whatsapp_app_secret, session_secret,
    ip_hash_pepper, media_sign_secret) are rejected at the API layer so
    they never reach the DB — those fields always come from env.

    Both the env-only half and the DB-override snapshot are cached;
    `bust_settings_cache()` clears both after admin writes so live
    updates take effect without a server restart.
    """
    base = _env_settings()
    overrides = _db_overrides()
    if not overrides:
        return base
    # Pydantic-settings' `__init__` reads env vars for any field not
    # explicitly provided — passing `Settings(smtp_user="DB")` would
    # still pull SMTP_USER from the environment, silently shadowing the
    # DB override. `model_validate(dict)` builds the model from the dict
    # directly with no env read, so the merged value is authoritative.
    merged = {**base.model_dump(), **overrides}
    return Settings.model_validate(merged)


def bust_settings_cache() -> None:
    """Clear cached env-based settings + the DB-override snapshot so
    DB overrides take effect.

    Call this from the settings API after writes, and from the app
    lifespan after `init_db()` so pre-DB cached values don't leak.
    """
    _env_settings.cache_clear()
    _db_overrides.cache_clear()
    get_settings.cache_clear()


def bust_settings_cache() -> None:
    """Clear cached env-based settings + the DB-override snapshot so
    DB overrides take effect.

    Call this from the settings API after writes, and from the app
    lifespan after `init_db()` so pre-DB cached values don't leak.
    """
    _env_settings.cache_clear()
    _db_overrides.cache_clear()
    get_settings.cache_clear()
