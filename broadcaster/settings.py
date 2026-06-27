"""Application settings loaded from environment variables.

Secrets must come from env (never the settings DB table). Non-secret
preferences may be overridden via the settings DB table at runtime.
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
