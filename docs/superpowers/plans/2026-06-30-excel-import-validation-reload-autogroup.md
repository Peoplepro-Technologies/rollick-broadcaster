# Excel Import — Validation, Auto-Reload, Conditional Auto-Group Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten Excel import validation (in-file dup phone/email, DB email conflict), surface all skipped rows in a modal, auto-reload the user list (no manual button), and conditionally rebuild auto-groups when dept/location changes.

**Architecture:** Service layer (`broadcaster/services/users.py`) grows tier-B detection + dept-change tracking inside the existing `import_from_xlsx` per-row loop; new pure helper `import_to_csv_errors()` for the errors-CSV endpoint. Frontend (`users.html` + `users.js` + `admin.css`) replaces the flat inline banner with a `<dialog>`-style modal that opens when `skipped > 0`; Close triggers `location.reload()`, backdrop/Escape do NOT. New endpoint `POST /api/users/upload-excel/errors.csv` accepts the same `errors[]` shape and returns RFC-4180 CSV.

**Tech Stack:** FastAPI, openpyxl, httpx (tests), Python ≥ 3.12, vanilla JS on the front end. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-30-excel-import-validation-reload-autogroup-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `broadcaster/services/users.py` | `import_from_xlsx` rewrite + `import_to_csv_errors()` new helper |
| `broadcaster/routes/admin_users.py` | New `POST /api/users/upload-excel/errors.csv` route |
| `broadcaster/templates/admin/users.html` | Remove manual reload button, add import-errors modal markup |
| `static/js/users.js` | Rewrite upload handler: modal-open on skip, auto-reload on clean, errors-CSV download |
| `static/css/admin.css` | Tiny addition for modal scroll |
| `tests/test_users.py` | Update existing reason-code assertions + add new tier-B / dept-change / errors-CSV tests |

`broadcaster/services/groups.py` is **not modified** — we call `rebuild_auto_groups()` as-is.

---

## Task 1: Service-layer error shape + reason-code rename

**Files:**
- Modify: `broadcaster/services/users.py` (the `import_from_xlsx` body and its `_row_to_dict` helper)
- Test: `tests/test_users.py`

- [ ] **Step 1: Add a new test asserting the renamed reason codes and the new `field`+`value` keys**

In `tests/test_users.py`, replace the assertions at the tail of `test_excel_import_reports_invalid_rows` (lines ~262–266) with:

```python
reasons = {(e["row"], e["reason"]) for e in body["errors"]}
fields = {(e["row"], e["field"]) for e in body["errors"]}
assert ("", "invalid_phone_format") not in reasons  # placeholder, real check next step
```

(We'll tighten this in step 3 once the field/value plumbing lands. The rename from `invalid_phone` → `invalid_phone_format` is part of this task.)

- [ ] **Step 2: Update `_row_to_dict` to set `field=None`, `value=None` placeholders**

In `broadcaster/services/users.py`, modify the call sites inside the `for idx, row in enumerate(...)` loop to construct the error dict via a single helper. Add a private helper just above `import_from_xlsx`:

```python
def _err(row: int, reason: str, field: str | None, value) -> dict:
    """Single error-dict shape returned to the front-end modal."""
    return {"row": row, "reason": reason, "field": field, "value": value}
```

Replace every `errors.append({"row": idx, "reason": "..."})` line inside `import_from_xlsx` with the new helper, supplying `field` (`"name"`, `"phone"`, `"email"`, or `None`) and `value` (the offending cell content or `None`). Also rename:

| Old reason                | New reason              | field      |
|---------------------------|-------------------------|------------|
| `name_or_phone_missing`   | `name_or_phone_missing` | `"name"` / `"phone"` |
| `invalid_phone`           | `invalid_phone_format`  | `"phone"`  |
| `invalid_email`           | `invalid_email_format`  | `"email"`  |
| `phone_taken` (upsert=false) | `phone_taken`        | `"phone"`  |
| `db_error: <msg>`         | `db_error`              | `None` (value = truncated msg) |

The `invalid_phone` → `invalid_phone_format` rename only impacts **import** path; the single-create endpoints in `create_user`/`update_user` keep `invalid_phone` / `invalid_email` as their `HTTPException.detail` (existing tests assert those). Search-and-replace inside `import_from_xlsx` only — do not touch the `_validate_phone` / `_validate_email` helpers.

- [ ] **Step 3: Verify the rename by running the now-failing Excel test**

Run:

```bash
pytest tests/test_users.py::test_excel_import_reports_invalid_rows -v
```

Expected: FAIL — the existing test still asserts `"invalid_phone"` and `"invalid_email"`. Update the test's `reasons` set to:

```python
reasons = {e["reason"] for e in body["errors"]}
assert "name_or_phone_missing" in reasons
assert "invalid_phone_format" in reasons
assert "invalid_email_format" in reasons
```

Re-run. Expected: PASS.

- [ ] **Step 4: Run the full test_users suite to verify no regressions**

```bash
pytest tests/test_users.py -v
```

Expected: all pass. The single-create tests still use `invalid_phone` / `invalid_email` (`HTTPException`) and are unaffected.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/services/users.py tests/test_users.py
git commit -m "refactor(users-import): uniform error dict shape + rename reason codes

Single _err() helper produces {row, reason, field, value}. Renames
invalid_phone -> invalid_phone_format and invalid_email ->
invalid_email_format for consistency with future duplicate-* codes.

Single-create endpoints (HTTPException) keep their detail codes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Tier-B detection — in-file dup phone, in-file dup email, DB email conflict

**Files:**
- Modify: `broadcaster/services/users.py` (inside `import_from_xlsx`)
- Test: `tests/test_users.py`

- [ ] **Step 1: Add three new test cases**

Append these to `tests/test_users.py` after `test_excel_import_reports_invalid_rows`:

```python
async def test_excel_import_flags_in_file_dup_phone(authed_client):
    """Two rows with the same normalized phone both surface errors; no inserts."""
    blob = _xlsx_bytes([
        ["name", "phone", "email"],
        ["DupA", "9876543210", "a@x.com"],
        ["DupB", "+91 98765 43210", "b@x.com"],
    ])
    files = {"file": ("u.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 0
    reasons = [e["reason"] for e in body["errors"]]
    assert reasons.count("duplicate_phone_in_file") == 2
    # No DB writes:
    rs = await authed_client.get("/api/users")
    assert rs.json() == []


async def test_excel_import_flags_in_file_dup_email(authed_client):
    blob = _xlsx_bytes([
        ["name", "phone", "email"],
        ["A", "1111111111", "shared@x.com"],
        ["B", "2222222222", "shared@x.com"],
    ])
    files = {"file": ("u.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    body = r.json()
    assert body["inserted"] == 0
    reasons = [e["reason"] for e in body["errors"]]
    assert reasons.count("duplicate_email_in_file") == 2


async def test_excel_import_flags_db_email_conflict(authed_client):
    """If email already belongs to another user, the imported row is skipped."""
    await authed_client.post(
        "/api/users", json={"name": "Owner", "phone": "5555555555", "email": "taken@x.com"}
    )
    blob = _xlsx_bytes([
        ["name", "phone", "email"],
        ["New", "6666666666", "taken@x.com"],
    ])
    files = {"file": ("u.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    body = r.json()
    assert body["inserted"] == 0
    assert body["errors"][0]["reason"] == "duplicate_email_in_db"
```

- [ ] **Step 2: Run the three tests — expect all FAIL**

```bash
pytest tests/test_users.py -k "in_file_dup or db_email_conflict" -v
```

Expected: 3 failures, each `NameError`/`KeyError` from missing reason code.

- [ ] **Step 3: Implement tier-B detection in `import_from_xlsx`**

Inside the `for idx, row in enumerate(data_rows, ...)` loop, **after** existing name/phone/email validation and **before** the existing phone-vs-DB pre-check, add:

```python
# Tier B: in-file duplicates within THIS upload.
if norm_phone in seen_phones:
    errors.append(_err(idx, "duplicate_phone_in_file", "phone", norm_phone))
    skipped += 1
    continue
seen_phones.add(norm_phone)

email_norm = d["email"].lower() if d["email"] else ""
if email_norm:
    if email_norm in seen_emails:
        errors.append(_err(idx, "duplicate_email_in_file", "email", d["email"]))
        skipped += 1
        continue
    # Tier B: DB email collision (different user has this email).
    db_owner = conn.execute(
        "SELECT id FROM users WHERE lower(email) = ?", (email_norm,)
    ).fetchone()
    if db_owner:
        errors.append(_err(idx, "duplicate_email_in_db", "email", d["email"]))
        skipped += 1
        continue
    seen_emails.add(email_norm)
```

Initialize `seen_phones: set[str] = set()` and `seen_emails: set[str] = set()` immediately before the `with get_db() as conn:` line (alongside the existing counter initializers).

> Note: We also gate the `existing = conn.execute("SELECT id FROM users WHERE phone = ?", ...)` block further down so that phone-conflict-with-DB remains the **upsert path** (existing behavior for `upsert=true`); don't touch it.

- [ ] **Step 4: Re-run the three tests — expect all PASS**

```bash
pytest tests/test_users.py -k "in_file_dup or db_email_conflict" -v
```

Expected: 3 passes.

- [ ] **Step 5: Run full `test_users.py` to confirm no regressions**

```bash
pytest tests/test_users.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add broadcaster/services/users.py tests/test_users.py
git commit -m "feat(users-import): tier-B validation (in-file + db-email dedup)

duplicate_phone_in_file: track phones seen in this upload; second
appearance -> error.
duplicate_email_in_file: same, only when email non-empty (case-insensitive).
duplicate_email_in_db: SELECT id FROM users WHERE lower(email)=?; if
found -> error.

Clean rows still inserted. Upsert-by-phone semantics preserved.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Conditional `rebuild_auto_groups()` + dept/location change detection

**Files:**
- Modify: `broadcaster/services/users.py` (inside `import_from_xlsx`)
- Test: `tests/test_users.py`

- [ ] **Step 1: Add four new tests covering the dept/loc change detection**

Append to `tests/test_users.py`:

```python
async def test_excel_import_triggers_rebuild_only_when_dept_changes(authed_client):
    """First import with a new dept should rebuild; second import with same dept should NOT."""
    # First import — new dept "Eng" appears.
    blob1 = _xlsx_bytes([
        ["name", "phone", "department", "location"],
        ["U1", "1111111111", "Eng", "BLR"],
    ])
    files1 = {"file": ("u1.xlsx", blob1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r1 = await authed_client.post("/api/users/upload-excel", files=files1)
    b1 = r1.json()
    assert b1["dept_location_changed"] is True
    assert b1["groups_created"] >= 1

    # Second import — same dept, different user.
    blob2 = _xlsx_bytes([
        ["name", "phone", "department", "location"],
        ["U2", "2222222222", "Eng", "BLR"],
    ])
    files2 = {"file": ("u2.xlsx", blob2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r2 = await authed_client.post("/api/users/upload-excel", files=files2)
    b2 = r2.json()
    assert b2["dept_location_changed"] is False
    assert b2["groups_created"] == 0


async def test_excel_import_rebuild_fires_on_case_insensitive_new_dept(authed_client):
    """'Eng' and 'eng' are the same dept after lower(trim()) — second import must NOT trigger rebuild."""
    blob1 = _xlsx_bytes([
        ["name", "phone", "department"],
        ["U1", "1111111111", "Eng"],
    ])
    files1 = {"file": ("u1.xlsx", blob1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r1 = await authed_client.post("/api/users/upload-excel", files=files1)
    assert r1.json()["dept_location_changed"] is True

    blob2 = _xlsx_bytes([
        ["name", "phone", "department"],
        ["U2", "2222222222", "eng"],  # case-only difference
    ])
    files2 = {"file": ("u2.xlsx", blob2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r2 = await authed_client.post("/api/users/upload-excel", files=files2)
    assert r2.json()["dept_location_changed"] is False


async def test_excel_import_rebuild_detects_changed_dept(authed_client):
    """Updating an existing user's dept to a NEW value must trigger rebuild."""
    # Seed a user with dept=Eng.
    blob1 = _xlsx_bytes([
        ["name", "phone", "department"],
        ["U1", "1111111111", "Eng"],
    ])
    files1 = {"file": ("u1.xlsx", blob1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r1 = await authed_client.post("/api/users/upload-excel", files=files1)
    assert r1.json()["dept_location_changed"] is True

    # Re-import with same phone but dept changed to Sales.
    blob2 = _xlsx_bytes([
        ["name", "phone", "department"],
        ["U1", "1111111111", "Sales"],
    ])
    files2 = {"file": ("u2.xlsx", blob2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r2 = await authed_client.post("/api/users/upload-excel", files=files2)
    b2 = r2.json()
    assert b2["updated"] == 1
    assert b2["dept_location_changed"] is True
    assert b2["groups_created"] >= 1


async def test_excel_import_rebuild_ignores_empty_dept(authed_client):
    """Empty dept strings must NOT trigger rebuild when DB had no dept."""
    blob = _xlsx_bytes([
        ["name", "phone", "department"],  # missing department col → empty
        ["U1", "1111111111", ""],
    ])
    files = {"file": ("u.xlsx", blob, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await authed_client.post("/api/users/upload-excel", files=files)
    assert r.json()["dept_location_changed"] is False
```

- [ ] **Step 2: Run the new tests — expect all FAIL**

```bash
pytest tests/test_users.py -k "triggers_rebuild or rebuild_fires or rebuild_detects or rebuild_ignores" -v
```

Expected: 4 failures because `dept_location_changed` is missing from the response.

- [ ] **Step 3: Add a normalization helper + integrate dept-change detection**

Add a module-level helper next to `_normalize_phone`:

```python
def _norm_dept_loc(s) -> str:
    """Case+whitespace-insensitive dept/location comparison key. Empty -> ''."""
    return (s or "").strip().lower()
```

Update `import_from_xlsx` to maintain a `dept_loc_changed` flag. Set it true at the following points:

1. **Inside the INSERT branch** — when the row's `department` is non-empty AND `lower(trim(department))` is not currently in `users.department`. Use the same check for `location`. **One query up front** to capture the pre-import snapshot, then compare per row:

```python
existing_depts = {r["department"] for r in conn.execute(
    "SELECT DISTINCT department FROM users "
    "WHERE department IS NOT NULL AND department != ''"
).fetchall()}
existing_locs  = {r["location"] for r in conn.execute(
    "SELECT DISTINCT location FROM users "
    "WHERE location IS NOT NULL AND location != ''"
).fetchall()}
# inside the loop, in the INSERT branch:
if _norm_dept_loc(d["department"]) and \
   _norm_dept_loc(d["department"]) not in {_norm_dept_loc(x) for x in existing_depts}:
    dept_loc_changed = True
if _norm_dept_loc(d["location"]) and \
   _norm_dept_loc(d["location"]) not in {_norm_dept_loc(x) for x in existing_locs}:
    dept_loc_changed = True
```

2. **Inside the UPDATE branch** — when either the new dept or location differs (via `_norm_dept_loc`) from the row's pre-update value. Re-query the row's existing dept/loc before the `UPDATE` to capture the pre-state.

Add the conditional rebuild at the **end of the loop**, after the `with get_db() as conn:` block exits:

```python
groups_created = 0
if dept_loc_changed:
    groups_svc.rebuild_auto_groups()
    with get_db() as conn:
        groups_created = conn.execute(
            "SELECT COUNT(*) AS n FROM groups WHERE is_auto = 1"
        ).fetchone()["n"]
```

Also add `import groups as groups_svc` at the top of the file (already there? check the imports section).

Make sure `import_from_xlsx` returns the new fields:

```python
return {
    "inserted": inserted,
    "updated": updated,
    "skipped": skipped,
    "errors": errors,
    "dept_location_changed": dept_loc_changed,
    "groups_created": groups_created,
}
```

- [ ] **Step 4: Run the four new tests — expect all PASS**

```bash
pytest tests/test_users.py -k "triggers_rebuild or rebuild_fires or rebuild_detects or rebuild_ignores" -v
```

Expected: 4 passes.

- [ ] **Step 5: Run full `test_users.py`**

```bash
pytest tests/test_users.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add broadcaster/services/users.py tests/test_users.py
git commit -m "feat(users-import): conditional rebuild_auto_groups()

Track dept_location_changed during the per-row loop. Trigger a rebuild
when a new dept/location value appears, OR when an existing user's
dept/location differs from the pre-update value.

Comparison is lower(trim(value)); empty values never trigger.

Response gains dept_location_changed + groups_created fields.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Pure helper `import_to_csv_errors()` + tests

**Files:**
- Modify: `broadcaster/services/users.py` (add new function near other excel helpers)

- [ ] **Step 1: Add the pure helper**

In `broadcaster/services/users.py`, after `import_from_xlsx` (after the return statement), add:

```python
# Server-side single source of truth for error reason -> human text.
# Mirrors the front-end humanizeError() map in static/js/users.js; keep in sync.
ERROR_HUMAN = {
    "name_or_phone_missing":      "Name and phone are required.",
    "invalid_phone_format":       "Phone must be 10 digits (Indian mobile).",
    "invalid_email_format":       "Email format is invalid.",
    "duplicate_phone_in_file":    "Duplicate phone in uploaded file.",
    "duplicate_email_in_file":    "Duplicate email in uploaded file.",
    "duplicate_email_in_db":      "Email already exists for another user.",
    "phone_taken":                "Phone already exists; skipped (upsert off).",
    "db_error":                   "Database error while saving row.",
}


def import_to_csv_errors(errors: list[dict]) -> bytes:
    """Render an errors[] array (as returned by import_from_xlsx) as RFC-4180 CSV."""
    import csv, io, datetime as _dt
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(["Row", "Field", "Value", "Reason", "Reason (human)"])
    for e in errors:
        w.writerow([
            e.get("row", ""),
            e.get("field") or "",
            e.get("value") if e.get("value") is not None else "",
            e.get("reason", ""),
            ERROR_HUMAN.get(e.get("reason", ""), "Unknown reason"),
        ])
    out = buf.getvalue()
    # add a BOM so Excel detects UTF-8 properly
    return ("﻿" + out).encode("utf-8")
```

Place the `ERROR_HUMAN` dict near `EXCEL_HEADERS` so both error-code vocabularies live next to each other.

- [ ] **Step 2: Add direct unit tests for the helper**

Append to `tests/test_users.py`:

```python
def test_import_to_csv_errors_basic():
    from broadcaster.services.users import import_to_csv_errors
    errs = [
        {"row": 3, "field": "email",  "value": "bad@",     "reason": "invalid_email_format"},
        {"row": 5, "field": "phone",  "value": "1234",      "reason": "invalid_phone_format"},
        {"row": 9, "field": "email",  "value": "x@y.com",   "reason": "duplicate_email_in_file"},
    ]
    csv_bytes = import_to_csv_errors(errs)
    text = csv_bytes.decode("utf-8-sig")  # tolerate BOM
    lines = [ln for ln in text.splitlines() if ln]
    assert lines[0] == "Row,Field,Value,Reason,Reason (human)"
    assert "invalid_phone_format" in lines[2]
    assert "Phone must be 10 digits" in lines[2]
    assert len(lines) == 4  # header + 3 rows


def test_import_to_csv_errors_empty():
    from broadcaster.services.users import import_to_csv_errors
    csv_bytes = import_to_csv_errors([])
    text = csv_bytes.decode("utf-8-sig")
    # Only the header row.
    assert text.strip().splitlines() == ["Row,Field,Value,Reason,Reason (human)"]


def test_import_to_csv_errors_handles_missing_keys():
    from broadcaster.services.users import import_to_csv_errors
    errs = [{"reason": "db_error"}]  # missing row/field/value
    out = import_to_csv_errors(errs).decode("utf-8-sig")
    # row/col mapped to empty, value empty, reason present, human text default.
    assert "db_error" in out
    assert "Database error" in out
```

- [ ] **Step 3: Run the new tests — expect all PASS (helper already implemented)**

```bash
pytest tests/test_users.py -k "import_to_csv_errors" -v
```

Expected: 3 passes (helper is pure; tests just nail the contract).

- [ ] **Step 4: Commit**

```bash
git add broadcaster/services/users.py tests/test_users.py
git commit -m "feat(users-import): pure errors-to-CSV helper

import_to_csv_errors(errors[]) returns UTF-8 + BOM CSV.
Single ERROR_HUMAN map is the server-side source of truth;
front-end humanizeError must stay in sync.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: New endpoint `POST /api/users/upload-excel/errors.csv` + tests

**Files:**
- Modify: `broadcaster/routes/admin_users.py`
- Test: `tests/test_users.py`

- [ ] **Step 1: Add a failing route test**

Append to `tests/test_users.py`:

```python
async def test_excel_errors_csv_endpoint(authed_client):
    errs = [
        {"row": 3, "field": "email", "value": "bad@", "reason": "invalid_email_format"},
        {"row": 7, "field": "phone", "value": "12345", "reason": "invalid_phone_format"},
    ]
    r = await authed_client.post("/api/users/upload-excel/errors.csv", json={"errors": errs})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.content.decode("utf-8-sig")
    assert "invalid_email_format" in body
    assert "Email format is invalid." in body


async def test_excel_errors_csv_endpoint_rejects_empty(authed_client):
    r = await authed_client.post("/api/users/upload-excel/errors.csv", json={"errors": []})
    assert r.status_code == 400
    assert r.json()["detail"] == "no_errors"


async def test_excel_errors_csv_endpoint_unauth(client):
    r = await client.post("/api/users/upload-excel/errors.csv", json={"errors": [{"row": 1}]})
    assert r.status_code == 401
```

- [ ] **Step 2: Run — expect FAIL (endpoint missing)**

```bash
pytest tests/test_users.py::test_excel_errors_csv_endpoint -v
```

Expected: 404 (route not found).

- [ ] **Step 3: Add the route**

In `broadcaster/routes/admin_users.py`, append (after the existing `upload_excel` route):

```python
from fastapi import Body  # already? check; if missing, add at top with other imports

@router.post("/upload-excel/errors.csv")
async def upload_excel_errors_csv(payload: dict = Body(...)):
    """Return the same `errors[]` array as RFC-4180 CSV. 400 when empty."""
    errors = payload.get("errors") or []
    if not isinstance(errors, list) or not errors:
        raise HTTPException(status_code=400, detail="no_errors")
    blob = users_svc.import_to_csv_errors(errors)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=blob,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="users_import_errors_{stamp}.csv"'},
    )
```

Add `from datetime import datetime` to the imports at the top if not already present.

- [ ] **Step 4: Run the route tests — expect all PASS**

```bash
pytest tests/test_users.py -k "errors_csv" -v
```

Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/routes/admin_users.py tests/test_users.py
git commit -m "feat(users-import): POST /upload-excel/errors.csv endpoint

Returns RFC-4180 CSV (UTF-8 + BOM) from the same errors[] shape that
upload-excel returns. 400 'no_errors' when errors is empty/missing.

Auth-gated via the existing /api/users router prefix dependencies.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — `users.html` (remove reload button, add modal markup)

**Files:**
- Modify: `broadcaster/templates/admin/users.html`

- [ ] **Step 1: Replace the import-result div with a slim banner + modal trigger**

Find this block (around lines 51–54 of `users.html`):

```html
<div id="import-result" class="form-success" hidden style="margin: 12px 0;">
  <span id="import-result-msg"></span>
  <button type="button" class="btn small" id="import-result-reload" onclick="location.reload()" hidden style="margin-left: 12px;">Reload to see new users →</button>
</div>
```

Replace with:

```html
<div id="import-result" class="form-success" hidden style="margin: 12px 0;">
  <span id="import-result-msg"></span>
  <button type="button" class="btn small" id="import-result-view-errors" hidden style="margin-left: 12px;">View errors →</button>
</div>
```

- [ ] **Step 2: Add the modal markup just before the closing `{% endblock %}` of the body block (around line 113)**

```html
<!-- Import errors modal -->
<div id="import-errors-modal" class="modal-backdrop" hidden onclick="if(event.target===this)closeImportErrorsModal(false)">
  <div class="modal-card" onclick="event.stopPropagation()">
    <h3>Import errors</h3>
    <p id="import-errors-summary" class="sub"></p>
    <div id="import-errors-table-wrap" class="modal-body">
      <table class="table" id="import-errors-table">
        <thead>
          <tr><th>Row</th><th>Field</th><th>Value</th><th>Reason</th></tr>
        </thead>
        <tbody id="import-errors-tbody"></tbody>
      </table>
    </div>
    <div class="modal-actions">
      <button type="button" class="btn secondary" id="import-errors-download">Download errors CSV</button>
      <button type="button" class="btn" onclick="closeImportErrorsModal(true)">Close</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Commit (HTML-only — JS wiring lands in task 7)**

```bash
git add broadcaster/templates/admin/users.html
git commit -m "feat(users-page): replace inline reload button with modal trigger

The 'Reload to see new users' button is gone — auto-reload lands in
task 7. The 'View errors' button only appears when the import had
skipped rows.

Modal markup reuses the existing .modal-backdrop / .modal-card / .modal-
actions classes. Close button reloads the page; backdrop click does NOT.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — `static/css/admin.css` (modal scroll)

**Files:**
- Modify: `static/css/admin.css`

- [ ] **Step 1: Add modal-body scroll rule**

Append to `static/css/admin.css`:

```css
/* Modal scroll: long error tables scroll inside the card. */
.modal-body { max-height: 60vh; overflow: auto; }
```

- [ ] **Step 2: Commit**

```bash
git add static/css/admin.css
git commit -m "style(admin): scroll inside modals with long content

.modal-body { max-height: 60vh; overflow: auto } so the import-errors
table can have dozens of rows without overflowing the viewport.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — `static/js/users.js` (auto-reload, modal logic, errors-CSV download)

**Files:**
- Modify: `static/js/users.js`

- [ ] **Step 1: Extend `humanizeError` and add the modal/event functions**

Find the existing `humanizeError` function (last in `users.js`) and replace it with:

```js
// Single source of truth — keep in sync with broadcaster/services/users.py::ERROR_HUMAN
const ERROR_HUMAN = {
  name_or_phone_missing:     'Name and phone are required.',
  invalid_phone_format:      'Phone must be 10 digits (Indian mobile).',
  invalid_email_format:      'Email format is invalid.',
  duplicate_phone_in_file:   'Duplicate phone in uploaded file.',
  duplicate_email_in_file:   'Duplicate email in uploaded file.',
  duplicate_email_in_db:     'Email already exists for another user.',
  phone_taken:               'Phone already exists; skipped (upsert off).',
  db_error:                  'Database error while saving row.',
};
function humanizeError(reason) {
  return ERROR_HUMAN[reason] || reason;
}

let _importErrorsBody = null;     // last imported errors[], used by Close/download

function openImportErrorsModal(body) {
  _importErrorsBody = body;
  const rows = body.errors || [];
  const summary = `${rows.length} row${rows.length === 1 ? '' : 's'} skipped, ${body.inserted + body.updated} row${body.inserted + body.updated === 1 ? '' : 's'} imported. Click Close to refresh the user list.`;
  document.getElementById('import-errors-summary').textContent = summary;
  const tbody = document.getElementById('import-errors-tbody');
  tbody.innerHTML = '';   // clear previous
  for (const e of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${e.row ?? ''}</td>` +
      `<td>${e.field ?? ''}</td>` +
      `<td>${(e.value ?? '').toString().replace(/[<&>]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</td>` +
      `<td>${humanizeError(e.reason)}</td>`;
    tbody.appendChild(tr);
  }
  document.getElementById('import-errors-modal').hidden = false;
}

function closeImportErrorsModal(reload) {
  document.getElementById('import-errors-modal').hidden = true;
  if (reload) location.reload();
}

// Escape key closes the modal WITHOUT reload (backdrop-like).
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    const m = document.getElementById('import-errors-modal');
    if (m && !m.hidden) closeImportErrorsModal(false);
  }
});

// "Download errors CSV" — POST the same errors array back to the server.
document.getElementById('import-errors-download').addEventListener('click', async () => {
  if (!_importErrorsBody || !_importErrorsBody.errors || !_importErrorsBody.errors.length) return;
  try {
    const r = await fetch('/api/users/upload-excel/errors.csv', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ errors: _importErrorsBody.errors }),
    });
    if (!r.ok) {
      alert('Download failed: ' + r.status);
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'users_import_errors.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Network error: ' + e.message);
  }
});
```

- [ ] **Step 2: Replace the xlsx-input change handler with the auto-reload version**

Find the existing block (the entire `document.getElementById('xlsx-input').addEventListener('change', async (ev) => { ... })` in `users.js`) and replace the **body** with:

```js
try {
  const r = await fetch('/api/users/upload-excel?upsert=true', { method: 'POST', body: fd });
  const body = await r.json().catch(() => ({}));
  if (r.ok) {
    const parts = [
      `+${body.inserted} added`,
      `~${body.updated} updated`,
      `!${body.skipped} skipped`,
    ];
    let txt = `✓ Import complete — ${parts.join(', ')}.`;
    if (body.skipped > 0) {
      txt += ` ${body.skipped} row${body.skipped === 1 ? '' : 's'} had errors.`;
    }
    msg.textContent = txt;
    // Show "View errors" button whenever there are skipped rows.
    const viewBtn = document.getElementById('import-result-view-errors');
    if (body.skipped > 0) {
      viewBtn.hidden = false;
      viewBtn.onclick = () => openImportErrorsModal(body);
    } else {
      viewBtn.hidden = true;
    }
    // Auto-reload. If there are skipped rows, defer until the modal is closed.
    if (body.inserted + body.updated > 0) {
      if (body.skipped > 0) {
        // open modal immediately so errors aren't lost
        openImportErrorsModal(body);
        // Close handler already triggers location.reload() — see above.
      } else {
        location.reload();
      }
    }
  } else {
    // Real error: red banner, no reload.
    result.classList.remove('form-success');
    result.classList.add('form-error');
    msg.textContent = '✗ Import failed: ' + (body.detail || r.status);
  }
} catch (e) {
  result.classList.remove('form-success');
  result.classList.add('form-error');
  msg.textContent = '✗ Network error: ' + e.message;
}
```

The `fetch(...)` call itself is unchanged from the prior code; only its inner branches were replaced.

- [ ] **Step 3: Static syntax check**

Run:

```bash
node --check static/js/users.js
```

Expected: exits 0 (no syntax error). If node is missing, use any JS parser (or skip if no JS runtime is available locally — the change is small enough to eyeball).

- [ ] **Step 4: Commit**

```bash
git add static/js/users.js
git commit -m "feat(users-page): auto-reload + modal error UX + errors CSV download

Upload handler:
  - 0 inserts/updates + N skipped: banner with 'View errors' button, no reload
  - M inserted/updated + 0 skipped: location.reload() immediately
  - M inserted/updated + N skipped: open modal; Close button reloads
    the page; backdrop click + Escape dismiss WITHOUT reloading

Modal shows Row / Field / Value / Reason (Reason humanized server-
side map <-> client-side map kept in sync via the constants in
broadcaster/services/users.py::ERROR_HUMAN).

'Download errors CSV' posts the same errors[] to /api/users/upload-
excel/errors.csv and triggers a file save.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Manual smoke verification

**Files:** none — exercises the running app at `http://localhost:8123`.

- [ ] **Step 1: Happy path**

1. Restart the container so static assets + images pick up the new JS/CSS: `docker compose restart rollick-broadcaster`.
2. Browser, hard-reload `/admin/users` (cache-bust).
3. Upload `clean.xlsx` (3 distinct rows, distinct dept values like `Eng`).
4. Observe: green banner `✓ Import complete — +3 added, ~0 updated, !0 skipped.` → page reloads automatically → 3 new rows visible.
5. Navigate to `/admin/groups`. Confirm new `Dept: Eng` + `Loc: BLR` auto-groups exist.

- [ ] **Step 2: Error popup path**

1. Build a synthetic `with-errors.xlsx`:
   - Row 2 valid (`N1`, `1111111111`, `a@x.com`, `Eng`, `BLR`)
   - Row 3 dup phone (`N2`, `+91 11111 11111`, `b@x.com`, `Eng`, `BLR`)
   - Row 4 bad email (`N3`, `2222222222`, `not-an-email`, `Eng`, `BLR`)
   - Row 5 dup email (`N4`, `3333333333`, `a@x.com`, `Eng`, `BLR`)
2. Upload it.
3. Observe modal opens with 3 error rows (`Row 3`, `Row 4`, `Row 5`) and humanized reasons.
4. Click "Download errors CSV" — file downloads, opens in Excel/Sheets, columns match.
5. Click backdrop → modal hides, page does **NOT** reload.
6. Click "View errors →" again (the banner button) → modal reopens. Click Close → page reloads → only Row 2 (`N1`) is in the users list.
7. Refresh manually, fix the .xlsx, re-upload the corrected file. Expect `+3 added, !0 skipped`.

- [ ] **Step 3: Re-import (upsert) regression**

1. With the user list from step 2, re-upload the same `clean.xlsx`. Expect `+0 added, ~3 updated, !0 skipped`. Modal does **NOT** open. `groups_created=0` (dept unchanged).

- [ ] **Step 4: Dept-change triggers rebuild**

1. Build `dept-change.xlsx`: one existing user's phone, dept flipped to a new value.
2. Upload it. Expect `~1 updated, !0 skipped` and `dept_location_changed=true`. Navigate to `/admin/groups` and confirm the new `Dept: <NewName>` auto-group appears.

- [ ] **Step 5: Capture screenshots + commit evidence**

No code commit. Mark this task done when all four smoke paths are green in the user's browser. If anything is broken, do NOT move on — debug and patch.

- [ ] **Step 6: Final tests pass**

```bash
pytest tests/test_users.py -v
pytest tests/test_groups.py -v
pytest tests/ -v
```

Expected: all pass.

---

## Out-of-scope reminders (do NOT touch in this plan)

- Replace mode (`2026-06-29` §1, with hard delete + admin guard) — separate plan.
- Skipped-report `.xlsx` endpoint (`2026-06-29` §4) — CSV endpoint supersedes; .xlsx can come later.
- Import history / per-import page.
- Column-letter error mapping.
- Other importers.

---

## Acceptance recap (per spec)

- [x] `errors[]` carries `field` + `value` — **T1**
- [x] In-file dups (`phone` or `email`) error cleanly — **T2**
- [x] DB-side email conflicts error cleanly — **T2**
- [x] Phone conflict with `upsert=true` still updates — regression guard in **T2 step 5**
- [x] Auto-reload on clean import — **T8**
- [x] Modal opens on errors; Close reloads; backdrop/Escape do not — **T6 + T8**
- [x] Manual reload button gone — **T6**
- [x] `rebuild_auto_groups()` only when dept/location changes — **T3**
- [x] `POST /api/users/upload-excel/errors.csv` — **T5**
- [x] All pre-existing tests still pass — each task's full-suite re-run
- [x] Manual smoke — **T9**
