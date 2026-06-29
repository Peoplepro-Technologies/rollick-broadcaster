"""Phase 1d — Content CRUD + media upload."""
from __future__ import annotations

import io

import pytest


async def _login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


# ── Text ──────────────────────────────────────────────────────

async def test_create_text(authed_client):
    r = await authed_client.post(
        "/api/content/text", json={"caption": "Greeting", "body": "Hello there!"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "text"
    assert body["content_data"] == "Hello there!"
    assert body["caption"] == "Greeting"


async def test_create_text_requires_body(authed_client):
    r = await authed_client.post("/api/content/text", json={"caption": "x"})
    assert r.status_code == 400
    assert r.json()["detail"] == "body_required"


async def test_list_includes_text(authed_client):
    await authed_client.post("/api/content/text", json={"body": "A"})
    await authed_client.post("/api/content/text", json={"body": "B"})
    r = await authed_client.get("/api/content")
    body = r.json()
    assert len(body) == 2
    assert all(c["content_type"] == "text" for c in body)


# ── Media upload ──────────────────────────────────────────────

async def test_upload_media_persists_file(authed_client, tmp_path, monkeypatch):
    # Redirect the upload dir to tmp_path for the test
    monkeypatch.chdir(tmp_path)
    files = {"file": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
    r = await authed_client.post("/api/content/media", files=files, data={"caption": "greet"})
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "media"
    assert body["mime_type"] == "text/plain"
    assert body["file_name"] == "hello.txt"
    assert body["file_size"] == 11
    # The file should be on disk under uploads/
    from pathlib import Path
    rel = body["content_data"]
    assert Path(rel).exists()
    assert Path(rel).read_bytes() == b"hello world"


async def test_upload_media_rejects_empty(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
    r = await authed_client.post("/api/content/media", files=files)
    assert r.status_code == 400
    assert r.json()["detail"] == "empty_file"


async def test_upload_media_rewrites_filename_for_safety(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Filename with path traversal — should be sanitized to just basename
    files = {"file": ("../../etc/passwd.txt", io.BytesIO(b"x"), "text/plain")}
    r = await authed_client.post("/api/content/media", files=files)
    assert r.status_code == 200
    body = r.json()
    assert ".." not in body["content_data"]
    assert "/" in body["content_data"]  # uploads/...


# ── Delete ────────────────────────────────────────────────────

async def test_delete_text(authed_client):
    cr = await authed_client.post("/api/content/text", json={"body": "x"})
    cid = cr.json()["id"]
    r = await authed_client.delete(f"/api/content/{cid}")
    assert r.status_code == 200
    r2 = await authed_client.get(f"/api/content/{cid}")
    assert r2.status_code == 404


async def test_delete_media_removes_file(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("a.txt", io.BytesIO(b"data"), "text/plain")}
    cr = await authed_client.post("/api/content/media", files=files)
    body = cr.json()
    from pathlib import Path
    rel_path = Path(body["content_data"])
    assert rel_path.exists()

    r = await authed_client.delete(f"/api/content/{body['id']}")
    assert r.status_code == 200
    assert not rel_path.exists()


# ── File serve ────────────────────────────────────────────────

async def test_serve_media_returns_file(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("x.bin", io.BytesIO(b"binary-content"), "application/octet-stream")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]
    r = await authed_client.get(f"/api/content/file/{cid}")
    assert r.status_code == 200
    assert r.content == b"binary-content"


async def test_serve_media_resolves_when_upload_root_differs_from_cwd(
    authed_client, tmp_path, monkeypatch
):
    """Regression: when the upload root (sibling of the DB file) lives
    in a directory OTHER than the process CWD — e.g. /data/uploads while
    CWD is /app inside the container — the download endpoint must still
    serve the file. Pre-fix this returned 404 file_missing because
    `Path("uploads/xxx")` was resolved against CWD instead of the upload
    root."""
    from broadcaster.settings import get_settings

    # Put the DB and the upload dir under tmp_path, but chdir to a
    # *different* tmp_path so the bug condition is reproduced.
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", str(db_dir / "broadcaster.db"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    files = {"file": ("video.mp4", io.BytesIO(b"video-bytes"), "video/mp4")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]

    # The download endpoint must succeed even though CWD's 'uploads/'
    # is empty — the file lives under db_dir / 'uploads' instead.
    r = await authed_client.get(f"/api/content/file/{cid}")
    assert r.status_code == 200, r.text
    assert r.content == b"video-bytes"

    # And the stored path must be absolute (not CWD-relative).
    from broadcaster.db import get_db
    with get_db() as conn:
        stored = conn.execute(
            "SELECT content_data FROM content WHERE id = ?", (cid,)
        ).fetchone()["content_data"]
    from pathlib import Path
    assert Path(stored).is_absolute(), f"expected absolute path, got: {stored}"


async def test_serve_media_404_for_text(authed_client):
    cr = await authed_client.post("/api/content/text", json={"body": "x"})
    cid = cr.json()["id"]
    r = await authed_client.get(f"/api/content/file/{cid}")
    assert r.status_code == 404


# ── Auth ──────────────────────────────────────────────────────

async def test_content_requires_auth(client):
    r = await client.get("/api/content")
    assert r.status_code == 401
