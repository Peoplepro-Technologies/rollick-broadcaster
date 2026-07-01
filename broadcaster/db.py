"""SQLite database helpers.

Schema is the unified data model from BUILD_PLAN.md §3. One file (`broadcaster.db`).
Foreign keys + WAL mode enabled for concurrency.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from broadcaster.settings import get_settings


SCHEMA = """
-- ── Subscribers ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  phone        TEXT NOT NULL,
  email        TEXT,
  department   TEXT,
  location     TEXT,
  is_active    INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone     ON users(phone);
CREATE INDEX        IF NOT EXISTS idx_users_dept      ON users(department) WHERE is_active=1;
CREATE INDEX        IF NOT EXISTS idx_users_location  ON users(location)   WHERE is_active=1;
CREATE INDEX        IF NOT EXISTS idx_users_created_at ON users(created_at);

-- ── Groups ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS groups (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL,
  criteria    TEXT,
  is_auto     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_groups_auto ON groups(is_auto);

CREATE TABLE IF NOT EXISTS group_memberships (
  group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id   INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_gm_user ON group_memberships(user_id);

-- ── Content library ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS content (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  content_type  TEXT NOT NULL,
  caption       TEXT,
  content_data  TEXT,
  file_name     TEXT,
  file_size     INTEGER,
  mime_type     TEXT,
  created_at    TEXT NOT NULL
);

-- ── Broadcasts ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broadcasts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  title            TEXT NOT NULL,
  category         TEXT NOT NULL DEFAULT 'General',
  message_text     TEXT,
  content_id       INTEGER REFERENCES content(id) ON DELETE SET NULL,
  delivery_channel TEXT NOT NULL DEFAULT 'whatsapp',
  scheduled_at     TEXT,
  sent_at          TEXT,
  status           TEXT NOT NULL DEFAULT 'draft',
  whatsapp_status  TEXT,
  email_status     TEXT,
  generate_links   INTEGER NOT NULL DEFAULT 1,
  created_by       TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broadcasts_status ON broadcasts(status);
CREATE INDEX IF NOT EXISTS idx_broadcasts_sched  ON broadcasts(scheduled_at);

CREATE TABLE IF NOT EXISTS broadcast_targets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
  group_id     INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  user_id      INTEGER REFERENCES users(id)  ON DELETE CASCADE,
  CHECK ((group_id IS NOT NULL) <> (user_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_bt_bcast ON broadcast_targets(broadcast_id);
CREATE INDEX IF NOT EXISTS idx_bt_user  ON broadcast_targets(user_id);
CREATE INDEX IF NOT EXISTS idx_bt_group ON broadcast_targets(group_id);

-- ── Per-subscriber links ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS broadcast_links (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  broadcast_id    INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
  user_id         INTEGER NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
  token           TEXT NOT NULL,
  short_code      TEXT,
  created_at      TEXT NOT NULL,
  expires_at      TEXT,
  revoked_at      TEXT,
  first_viewed_at TEXT,
  UNIQUE(broadcast_id, user_id),
  UNIQUE(token)
);
CREATE INDEX IF NOT EXISTS idx_bl_token ON broadcast_links(token);
CREATE INDEX IF NOT EXISTS idx_bl_bcast ON broadcast_links(broadcast_id);
CREATE INDEX IF NOT EXISTS idx_bl_user  ON broadcast_links(user_id);

-- ── View tracking ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS link_views (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id    INTEGER NOT NULL REFERENCES broadcast_links(id) ON DELETE CASCADE,
  viewed_at  TEXT NOT NULL,
  ip_hash    TEXT,
  ua_hash    TEXT,
  referrer   TEXT
);
CREATE INDEX IF NOT EXISTS idx_lv_link ON link_views(link_id);
CREATE INDEX IF NOT EXISTS idx_lv_time ON link_views(viewed_at);

-- ── Anonymous comments ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS comments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id      INTEGER NOT NULL REFERENCES broadcast_links(id) ON DELETE CASCADE,
  broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id)      ON DELETE CASCADE,
  body         TEXT NOT NULL,
  author_hint  TEXT,
  ip_hash      TEXT,
  status       TEXT NOT NULL DEFAULT 'visible',
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_link   ON comments(link_id);
CREATE INDEX IF NOT EXISTS idx_comments_bcast  ON comments(broadcast_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status, created_at DESC);

-- ── Settings K/V (non-secret only) ────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT
);

-- ── Admin users ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'super_admin',
  created_at    TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Context manager: yields a connection, commits on success, rolls back on error."""
    settings = get_settings()
    conn = _connect(settings.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables/indexes if they don't exist. Idempotent.

    Also runs column-level migrations on `admins` (the role column added
    in 2026-07-01's RBAC refactor). Idempotent on fresh installs.
    """
    settings = get_settings()
    conn = _connect(settings.database_url)
    try:
        conn.executescript(SCHEMA)
        _migrate_admins_role(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_admins_role(conn: sqlite3.Connection) -> None:
    """Ensure `admins.role` exists and no row has a NULL role.

    Fresh installs: column already exists from CREATE TABLE, UPDATE is
    a no-op (no NULLs).
    Legacy installs: ADD COLUMN adds role as nullable; UPDATE backfills
    all rows to super_admin so app code never sees NULL.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(admins)").fetchall()}
    if "role" not in cols:
        conn.execute("ALTER TABLE admins ADD COLUMN role TEXT")
    conn.execute(
        "UPDATE admins SET role='super_admin' WHERE role IS NULL"
    )
