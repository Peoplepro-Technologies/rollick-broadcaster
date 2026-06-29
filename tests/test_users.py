"""Phase 1b — Users CRUD + Excel."""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook


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


# ── Create ────────────────────────────────────────────────────

async def test_create_user_minimal(authed_client):
    r = await authed_client.post(
        "/api/users",
        json={"name": "Alice", "phone": "9876543210"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Alice"
    assert body["phone"] == "9876543210"
    assert body["is_active"] == 1
    assert "id" in body


async def test_create_user_full(authed_client):
    r = await authed_client.post(
        "/api/users",
        json={
            "name": "Bob",
            "phone": "1234567890",
            "email": "bob@example.com",
            "department": "Eng",
            "location": "BLR",
            "is_active": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "bob@example.com"
    assert body["department"] == "Eng"


async def test_create_user_requires_name(authed_client):
    r = await authed_client.post("/api/users", json={"phone": "1111111111"})
    assert r.status_code == 400
    assert r.json()["detail"] == "name_and_phone_required"


async def test_create_user_rejects_short_phone(authed_client):
    r = await authed_client.post("/api/users", json={"name": "X", "phone": "12345"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_phone"


async def test_create_user_rejects_non_digit_phone(authed_client):
    r = await authed_client.post("/api/users", json={"name": "X", "phone": "abcdefghij"})
    assert r.status_code == 400


async def test_create_user_rejects_bad_email(authed_client):
    r = await authed_client.post(
        "/api/users", json={"name": "X", "phone": "2222222222", "email": "not-an-email"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_email"


async def test_create_user_rejects_duplicate_phone(authed_client):
    p = {"name": "A", "phone": "3333333333"}
    r1 = await authed_client.post("/api/users", json=p)
    assert r1.status_code == 200
    r2 = await authed_client.post("/api/users", json={**p, "name": "B"})
    assert r2.status_code == 409
    assert r2.json()["detail"] == "phone_taken"


# ── Phone normalization ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("9876543210",   "9876543210"),   # raw 10 digits
    ("+91 98765 43210", "9876543210"),  # +91 + spaces
    ("+91-9876543210",  "9876543210"),  # +91 + dashes
    ("919876543210",    "9876543210"),  # +91 no separator
    ("09876543210",     "9876543210"),  # leading 0
    ("(987) 654-3210",  "9876543210"),  # parens + dashes
    ("  +91 98765 43210  ", "9876543210"),  # whitespace
])
async def test_create_user_normalizes_phone(authed_client, raw, expected):
    r = await authed_client.post("/api/users", json={"name": f"User-{expected}", "phone": raw})
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == expected


@pytest.mark.parametrize("raw", ["abc", "12345", "98765", "+91 12", "12"])
async def test_create_user_rejects_unparseable_phone(authed_client, raw):
    r = await authed_client.post("/api/users", json={"name": "X", "phone": raw})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_phone"


async def test_excel_import_normalizes_phones(authed_client):
    """Real-world xlsx with +91 prefixes must import successfully."""
    blob = _xlsx_bytes([
        ["name", "phone", "email", "department", "location", "is_active"],
        ["Im1", "+91 98765 43210", "i1@x.com", "Eng", "BLR", "active"],
        ["Im2", "919876543211",    "i2@x.com", "Eng", "BLR", "active"],
        ["Im3", "(987) 654-3212",  "i3@x.com", "Eng", "BLR", "active"],
    ])
    files = {"file": ("users.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 3
    assert body["skipped"] == 0
    assert body["errors"] == []

    # Verify the phones were stored normalised.
    r = await authed_client.get("/api/users", params={"q": "Im"})
    phones = sorted(u["phone"] for u in r.json())
    assert phones == ["9876543210", "9876543211", "9876543212"]

    # Cleanup.
    for u in r.json():
        await authed_client.delete(f"/api/users/{u['id']}")


# ── List / get ────────────────────────────────────────────────

async def test_list_users_returns_created(authed_client):
    for n, p in [("A", "4000000001"), ("B", "4000000002")]:
        await authed_client.post("/api/users", json={"name": n, "phone": p})
    r = await authed_client.get("/api/users")
    assert r.status_code == 200
    body = r.json()
    phones = {u["phone"] for u in body}
    assert {"4000000001", "4000000002"}.issubset(phones)


async def test_list_users_search_by_name(authed_client):
    await authed_client.post("/api/users", json={"name": "Alice", "phone": "5000000001"})
    await authed_client.post("/api/users", json={"name": "Bob", "phone": "5000000002"})
    r = await authed_client.get("/api/users", params={"q": "ali"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Alice"


async def test_list_users_filter_active(authed_client):
    await authed_client.post("/api/users", json={"name": "On", "phone": "6000000001", "is_active": True})
    await authed_client.post("/api/users", json={"name": "Off", "phone": "6000000002", "is_active": False})
    r = await authed_client.get("/api/users", params={"active_only": "true"})
    names = {u["name"] for u in r.json()}
    assert "On" in names
    assert "Off" not in names


# ── Update / delete ───────────────────────────────────────────

async def test_update_user(authed_client):
    cr = await authed_client.post("/api/users", json={"name": "X", "phone": "7000000001"})
    uid = cr.json()["id"]
    r = await authed_client.patch(f"/api/users/{uid}", json={"department": "Sales"})
    assert r.status_code == 200
    assert r.json()["department"] == "Sales"


async def test_update_user_to_duplicate_phone_conflicts(authed_client):
    a = await authed_client.post("/api/users", json={"name": "A", "phone": "8000000001"})
    b = await authed_client.post("/api/users", json={"name": "B", "phone": "8000000002"})
    r = await authed_client.patch(f"/api/users/{b.json()['id']}", json={"phone": "8000000001"})
    assert r.status_code == 409


async def test_delete_user(authed_client):
    cr = await authed_client.post("/api/users", json={"name": "X", "phone": "9000000001"})
    uid = cr.json()["id"]
    r = await authed_client.delete(f"/api/users/{uid}")
    assert r.status_code == 200
    r2 = await authed_client.get(f"/api/users/{uid}")
    assert r2.status_code == 404


# ── Auth gating ───────────────────────────────────────────────

async def test_users_endpoints_require_auth(client):
    r = await client.get("/api/users")
    assert r.status_code == 401
    r = await client.post("/api/users", json={"name": "X", "phone": "1234567890"})
    assert r.status_code == 401


# ── Excel ─────────────────────────────────────────────────────

def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_excel_import_upserts(authed_client):
    blob = _xlsx_bytes([
        ["name", "phone", "email", "department", "location", "is_active"],
        ["Imp1", "1111111111", "i1@x.com", "Eng", "BLR", "active"],
        ["Imp2", "2222222222", "", "Sales", "MUM", "1"],
    ])
    files = {"file": ("users.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 2
    assert body["updated"] == 0

    # Re-upload with one phone changed: should update one, insert one new.
    blob2 = _xlsx_bytes([
        ["name", "phone", "email", "department", "location", "is_active"],
        ["Imp1-renamed", "1111111111", "i1@x.com", "Eng", "BLR", "active"],
        ["Imp3", "3333333333", "", "Ops", "DEL", "1"],
    ])
    files2 = {"file": ("users.xlsx", blob2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r2 = await authed_client.post("/api/users/upload-excel", files=files2)
    body2 = r2.json()
    assert body2["updated"] == 1
    assert body2["inserted"] == 1


async def test_excel_import_reports_invalid_rows(authed_client):
    blob = _xlsx_bytes([
        ["name", "phone"],
        ["", "12345"],                # missing name + bad phone
        ["OK", "12345"],              # bad phone
        ["OK2", "9999999999", "bad"], # too many cells is fine; this is actually valid
    ])
    # Fix last row: invalid email
    blob = _xlsx_bytes([
        ["name", "phone", "email"],
        ["", "12345"],
        ["OK", "12345"],
        ["OK2", "9999999999", "not-an-email"],
    ])
    files = {"file": ("users.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    body = r.json()
    assert body["inserted"] == 0
    assert body["skipped"] == 3
    reasons = {e["reason"] for e in body["errors"]}
    assert "name_or_phone_missing" in reasons
    assert "invalid_phone" in reasons
    assert "invalid_email" in reasons


async def test_excel_export(authed_client):
    await authed_client.post("/api/users", json={"name": "Exp", "phone": "1212121212"})
    r = await authed_client.get("/api/users/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "attachment" in r.headers["content-disposition"]
    # The body is a binary .xlsx (zip-compressed); parse it to verify content.
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    ws = wb.active
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    assert any(row[0] == "Exp" and row[1] == "1212121212" for row in rows)


async def test_excel_template(authed_client):
    """Blank template has only headers + example rows, no live users."""
    # Seed a user that should NOT appear in the template.
    await authed_client.post("/api/users", json={"name": "LiveUser", "phone": "1313131313"})
    r = await authed_client.get("/api/users/template")
    assert r.status_code == 200
    assert "users_template.xlsx" in r.headers["content-disposition"]
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    ws = wb.active
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    # Header row + 2 example rows only — no live users.
    assert rows[0] == ["name", "phone", "email", "department", "location", "is_active"]
    assert len(rows) == 3
    # The live user must not be present.
    assert not any(row[0] == "LiveUser" for row in rows)
    # Clean up the seed.
    r = await authed_client.get("/api/users")
    for u in r.json():
        if u["name"] == "LiveUser":
            await authed_client.delete(f"/api/users/{u['id']}")
