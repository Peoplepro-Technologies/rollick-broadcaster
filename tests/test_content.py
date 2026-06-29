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
    tmp_path, monkeypatch
):
    """Regression: when the upload root lives in a directory OTHER than
    process CWD — e.g. /data/uploads while CWD is /app inside the
    container — `_resolve_content_path` must still find the file.

    Pre-fix, the download endpoint did `Path("uploads/xxx").exists()`
    which resolved against CWD instead of the upload root, returning
    404 file_missing for every fresh upload. The resolver now handles
    absolute paths (new rows) AND legacy data-dir-relative paths
    (pre-fix rows) so both styles keep working."""
    from broadcaster.services.content import _resolve_content_path

    # Plant a file at an absolute path that is NOT under CWD.
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    target = uploads / "video.mp4"
    target.write_bytes(b"video-bytes")

    # CWD's "uploads/" is empty so a CWD-relative resolve would miss.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    # Absolute stored paths resolve to themselves.
    resolved = _resolve_content_path(str(target))
    assert resolved.exists()
    assert resolved.read_bytes() == b"video-bytes"


async def test_serve_media_resolves_legacy_relative_path(
    tmp_path, monkeypatch
):
    """Pre-fix rows stored `content_data = 'uploads/<name>'` and the
    file actually lived at `<data_dir>/uploads/<name>` — the legacy
    path was meant relative to the DB's parent dir. The resolver must
    still find these files so existing broadcasts keep working after
    the upgrade."""
    from broadcaster.services.content import _resolve_content_path

    # The resolver looks at Path(database_url).parent for legacy rows,
    # so plant the file there. (Conftest sets DATABASE_URL to
    # tmp_path/test.db; legacy = tmp_path / 'uploads/legacy.mp4'.)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "legacy.mp4").write_bytes(b"legacy-bytes")

    # CWD-relative resolution would miss because CWD's uploads/ is empty.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = _resolve_content_path("uploads/legacy.mp4")
    assert resolved.exists(), f"resolver failed for legacy path: {resolved}"
    assert resolved.read_bytes() == b"legacy-bytes"


async def test_serve_media_404_for_text(authed_client):
    cr = await authed_client.post("/api/content/text", json={"body": "x"})
    cid = cr.json()["id"]
    r = await authed_client.get(f"/api/content/file/{cid}")
    assert r.status_code == 404


# ── Auth ──────────────────────────────────────────────────────

async def test_content_requires_auth(client):
    r = await client.get("/api/content")
    assert r.status_code == 401
