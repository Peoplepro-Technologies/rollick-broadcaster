"""Content library — reusable text snippets + uploaded media.

Media files are stored under `uploads/` with a uuid-prefixed filename to
avoid collisions and path traversal. The original filename and mime type
are recorded on the `content` row for display.

For v1 there are no quotas and no mime-type restriction. Phase 8 (hardening)
will add size caps and an admin-configurable allowlist.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile

from broadcaster.db import get_db
from broadcaster.settings import get_settings


UPLOAD_DIR_NAME = "uploads"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _upload_root() -> Path:
    return Path(get_settings().database_url).parent / UPLOAD_DIR_NAME


def _resolve_content_path(stored: str) -> Path:
    """Resolve a stored `content_data` string for a media row into an
    absolute filesystem path.

    New rows store absolute paths (see `create_media`). Pre-fix rows
    had a relative string like 'uploads/<uuid>_<name>' that was meant
    relative to the DATA directory (parent of the SQLite file) — the
    implicit container convention was that uploads lived next to the
    DB. We resolve relative paths against the data-dir first (legacy),
    then fall back to the upload root in case the convention ever
    diverged."""
    p = Path(stored)
    if p.is_absolute():
        return p
    # Legacy: 'uploads/...' was relative to the DB's parent dir.
    # E.g. DATABASE_URL=/data/broadcaster.db → file lived at
    # /data/uploads/<name>.
    data_dir = Path(get_settings().database_url).parent
    legacy = data_dir / p
    if legacy.exists():
        return legacy
    # Fallback: against the upload root directly.
    return _upload_root() / p


# ── List / get ────────────────────────────────────────────────

def list_content() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, content_type, caption, content_data, file_name, file_size, mime_type, created_at "
            "FROM content ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_content(cid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute(
            "SELECT id, content_type, caption, content_data, file_name, file_size, mime_type, created_at "
            "FROM content WHERE id = ?",
            (cid,),
        ).fetchone()
    return dict(r) if r else None


# ── Text ──────────────────────────────────────────────────────

def create_text(caption: Optional[str], body: str) -> dict:
    if not body or not body.strip():
        raise HTTPException(status_code=400, detail="body_required")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO content (content_type, caption, content_data, file_name, file_size, mime_type, created_at) "
            "VALUES ('text', ?, ?, NULL, NULL, NULL, ?)",
            (caption or None, body.strip(), _now()),
        )
    return get_content(cur.lastrowid)  # type: ignore[return-value]


# ── Media ─────────────────────────────────────────────────────

def create_media(file: UploadFile, caption: Optional[str]) -> dict:
    """Persist a media file. Returns the content row."""
    root = _upload_root()
    root.mkdir(parents=True, exist_ok=True)

    original = file.filename or "upload"
    safe_name = f"{uuid.uuid4().hex}_{Path(original).name}"
    # Strip any remaining path components
    safe_name = safe_name.replace("/", "_").replace("\\", "_")
    dest = root / safe_name

    contents = file.file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="empty_file")
    dest.write_bytes(contents)

    mime = file.content_type or "application/octet-stream"
    # Store an ABSOLUTE path so the download endpoint can resolve the
    # file regardless of process CWD (the upload root may live in a
    # completely different directory from where the server was started,
    # e.g. /data/uploads inside the container while CWD is /app).
    abs_path = str(dest.resolve())

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO content (content_type, caption, content_data, file_name, file_size, mime_type, created_at) "
            "VALUES ('media', ?, ?, ?, ?, ?, ?)",
            (caption or None, abs_path, original, len(contents), mime, _now()),
        )
    return get_content(cur.lastrowid)  # type: ignore[return-value]


# ── Delete ────────────────────────────────────────────────────

def delete_content(cid: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT content_type, content_data FROM content WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            return False
        # If media, also remove the file from disk
        if row["content_type"] == "media" and row["content_data"]:
            try:
                _resolve_content_path(row["content_data"]).unlink(missing_ok=True)
            except OSError:
                pass  # best-effort; row is the source of truth
        conn.execute("DELETE FROM content WHERE id = ?", (cid,))
    return True
