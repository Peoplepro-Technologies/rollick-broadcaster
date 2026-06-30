# Users Excel Import — Strict Validation, Auto-Reload, Conditional Auto-Group Rebuild

**Date:** 2026-06-30
**Status:** Draft (pending user review)
**Supersedes:** §4 "Frontend skip disclosure" of `2026-06-29-user-import-skip-report-design.md` (the user's 2026-06-29 spec is partially implemented today; the inline-disclosure UI is replaced by a modal in this spec).
**Defers:** Replace mode (destructive) from the 2026-06-29 spec — not in this iteration.

## Problem

The Excel import at `/admin/users` (1) leaves rows with duplicated values or DB-side email collisions silently inserted/updated, (2) shows only the first three skip reasons in a flat inline banner and then forces the admin to click "Reload" to see new rows, and (3) doesn't refresh auto-groups when dept/location values change, so the Groups page goes stale until someone clicks "↻ Rebuild Auto" manually.

The user's three requests on 2026-06-30:

1. Remove the "Reload to see new users →" button — the list should refresh automatically after a clean import; if there are skipped rows, the list refreshes when the user closes the error popup.
2. Validate email format, phone format, duplicate emails, and duplicate phones — both within the uploaded file and against the DB — and surface every bad row in a popup so the admin can fix them in their spreadsheet. Clean rows still get imported.
3. When an import changes the dept/location set, auto-trigger a rebuild of the auto groups (Dept: …, Loc: …, dept/loc combos) so the Groups page is current.

## Goals & non-goals

**Goals**
- After every Excel import, any skipped row appears in a modal with Row / Field / Value / Reason. Re-upload after fixing is the recovery path; no row in the file is silently dropped for an identifiable reason.
- No manual reload button. `location.reload()` fires automatically: immediately on clean imports, after modal close on import-with-errors.
- After every Excel import that changes the dept/location set, auto-groups (Dept/Loc/combo) reflect the new data. No separate "↻ Rebuild Auto" click needed.
- No database schema change. No new persistence beyond what `groups` already supports.

**Non-goals (this iteration)**
- Replace mode (destructive delete) — see 2026-06-29 spec §1; deferred.
- CSV download of skipped rows — defer to follow-up.
- Persisting import history.
- Highlighting failed rows to column letters.
- Soft-deactivate semantics.

## Design

### 0. Composition rule (read this first)

The three goals are orthogonal and must compose cleanly:

| Goal                           | Independent of                                       |
|--------------------------------|------------------------------------------------------|
| Strict validation              | Reload behavior, auto-group rebuild                  |
| Auto-reload                    | Whether the import had errors                        |
| Conditional auto-group rebuild | Whether anything else succeeded                      |

A clean import: validate → no errors → reload immediately → rebuild groups only if dept/location changed.
A dirty import: validate → errors → modal opens → user dismisses → reload → rebuild groups if dept/location changed.

Per-row validation status is the only coupling. The modal shows it. The reload button listens to it. The group rebuild checks for change after the import completes its row-by-row loop.

### 1. Validation rules (server-side, per row)

**Tier A — existing, keep**
- `name` non-empty after trim
- `phone` present + normalize to 10 digits (`_normalize_phone`: strips `+91`, leading `0`, parens, spaces, dashes)
- `email` optional + matches `^[^@\s]+@[^@\s]+\.[^@\s]+$`
- Phone conflict against the DB: existing pre-check (`SELECT id FROM users WHERE phone=?`) catches it before any INSERT; the DB UNIQUE constraint is the safety net, not the normal path.

**Tier B — new for Excel import**
- **`duplicate_phone_in_file`**: track phones seen so far in this upload; if a non-empty normalized phone matches an earlier-seen phone in the same file, error.
- **`duplicate_email_in_file`**: same, only when email is non-empty.
- **`duplicate_email_in_db`**: when email is non-empty, `SELECT id FROM users WHERE email = ?` against the DB; if any row, error.

**Tier C — keep upsert-by-phone semantics**
- `upsert=true` (default) silently updates an existing user on phone conflict — preserves the "re-import the export" workflow.
- `upsert=false` flips phone conflict to a `phone_taken` error (existing behavior, unchanged).

> Trade-off: tier C keeps an exemption. If the user wants stricter ("treat any DB phone conflict as error too"), flip tier C by passing a flag. Today's recommendation: keep.

### 2. Reason codes (single source of truth)

The 2026-06-29 spec §3 lists five codes; this spec adds three more. Server-side single map → front-end maps each to plain English via `humanizeError()`. Stays snake_case for machine-readability.

| `reason`                     | `field`   | `value` carries            | human-readable                          |
|------------------------------|-----------|----------------------------|-----------------------------------------|
| `name_or_phone_missing`      | name/phone| the empty one(s)           | "Name and phone are required."          |
| `invalid_phone_format`       | phone     | the raw string the admin typed | "Phone must be 10 digits (Indian mobile)." |
| `invalid_email_format`       | email     | the raw string             | "Email format is invalid."              |
| `duplicate_phone_in_file`    | phone     | the normalized 10-digit phone | "Duplicate phone in uploaded file."   |
| `duplicate_email_in_file`    | email     | the email string           | "Duplicate email in uploaded file."     |
| `duplicate_email_in_db`      | email     | the email string           | "Email already exists for another user."|
| `phone_taken`                | phone     | the normalized phone       | "Phone already exists; skipped (upsert=off)." |
| `db_error: <msg>`            | (varies)  | the error message (truncated 80ch) | "Database error."                |

The three renamed / new codes intentionally use snake_case suffixes (`_format`, `_in_file`, `_in_db`) so a future grep can pivot on them. The 2026-06-29 codes `invalid_phone` and `invalid_email` are renamed to `invalid_phone_format` / `invalid_email_format` so all "format-invalid" reasons share a suffix and all "duplicate" reasons share another.

### 3. Response shape — `POST /api/users/upload-excel`

```json
{
  "inserted": 12,
  "updated": 4,
  "skipped": 3,
  "errors": [
    {"row": 3, "field": "email", "value": "bad@",       "reason": "invalid_email_format"},
    {"row": 5, "field": "phone", "value": "1234",        "reason": "invalid_phone_format"},
    {"row": 9, "field": "email", "value": "x@y.com",     "reason": "duplicate_email_in_file"}
  ],
  "dept_location_changed": true,
  "groups_created": 6
}
```

- `inserted` / `updated` / `skipped` unchanged from today.
- `errors[]` adds `field` + `value` so the modal can display the offending cell content. (Compatible with — simpler than — the 2026-06-29 spec's `offending` / `original` / `reason_human` shape; if the 2026-06-29 spec gets implemented, this can be widened then.)
- New: `dept_location_changed` boolean and `groups_created` count.

### 4. Auto-reload logic (front-end)

```js
// inside static/js/users.js (xlsx-input change handler)
if (body.inserted + body.updated > 0) {
  if (body.skipped > 0) {
    openImportErrorsModal(body);     // user clicks Close → reload
  } else {
    location.reload();               // clean → reload now
  }
}
// otherwise (no inserts/updates): show banner, no reload, manual inspect
```

The current `<button id="import-result-reload">Reload to see new users →</button>` is removed from `users.html`. No state kept across reloads (next page view rebuilds the user list from the DB).

### 5. Error modal UX

Reuses the existing `modal-backdrop` / `modal-card` styling in `admin.css`. Opens only when `skipped > 0`.

```
┌── Import errors ─────────────────────────────────────┐
│ 3 rows skipped, 12 rows imported.                    │
│ Click Close to refresh the user list.                │
│ ┌─────┬─────────┬───────────┬──────────────────────┐ │
│ │ Row │ Field   │ Value     │ Reason               │ │
│ ├─────┼─────────┼───────────┼──────────────────────┤ │
│ │ 3   │ email   │ bad@…     │ Email format invalid │ │
│ │ 5   │ phone   │ 1234      │ Phone needs 10 digits│ │
│ │ 9   │ email   │ x@y.com   │ Duplicate in file    │ │
│ └─────┴─────────┴───────────┴──────────────────────┘ │
│                                                      │
│         [ Download errors CSV ]    [ Close ]         │
└──────────────────────────────────────────────────────┘
```

Behaviors:

- Modal opens centered, scrollable inside (`.modal-card` gets `max-height: 60vh; overflow: auto` on the table only — small CSS addition).
- Close button: hides modal, then `location.reload()`.
- Click backdrop or press `Escape`: hides modal WITHOUT reload (user opts out — keeps their view).
- "Download errors CSV" → POSTs `{errors: body.errors}` to a new `POST /api/users/upload-excel/errors.csv` and triggers a download. (See §6.1.)
- Reasons rendered via the front-end `humanizeError(reason)` mapper. Falls back to the raw code with `unknown_reason` if unmapped.

### 6. New endpoints

#### 6.1 — `POST /api/users/upload-excel/errors.csv`

Accepts the same `errors` shape returned from the upload endpoint:

```
Content-Type: application/json
Body: { "errors": [ {row, field, value, reason}, … ] }
Response:
  200 text/csv
       Content-Disposition: attachment; filename="users_import_errors_YYYYMMDD-HHMMSS.csv"
  400 {"detail": "no_errors"} when the array is empty
```

Pure function over request body — does NOT re-read stored state. CSV columns: `Row, Field, Value, Reason, Reason (human)`. UTF-8, header row, RFC-4180 quoting. Reason-human mapping identical to the modal's mapper (server-side, single source of truth).

### 7. Conditional auto-group rebuild

Inside `import_from_xlsx`, maintain one boolean across the per-row loop: `dept_loc_changed = False`.

Set it true when:

1. A NEW user is inserted whose **non-empty** `department` (or `location`) value does not currently exist in `users.department` (`SELECT EXISTS` before insert), normalized to **lower(trim())** for the existence check so `"Bangalore"` and `"bangalore"` count as the same value, OR
2. An existing user is updated and the new `department`/`location` (compared via `lower(trim(...))`) differs from the pre-update value of that column.

Empty / NULL values never trigger the flag — they're "no value", not "new value".

After the loop, if `dept_loc_changed`:

```python
from broadcaster.services import groups as groups_svc
groups_svc.rebuild_auto_groups()    # existing function, groups.py:177
# re-count via SELECT COUNT(*) WHERE is_auto=1 to fill groups_created
```

If false → skip. Saves a full DELETE+INSERT cycle on every name/phone-only edit.

Response carries `dept_location_changed: bool` and `groups_created: int` (0 when skipped).

This piggybacks on the **existing** `rebuild_auto_groups()` — no new group-creation logic, no schema change, no new endpoint. Only the user-import path now triggers it.

### 8. Files / surfaces touched

| File                                                  | Change                                              |
|-------------------------------------------------------|-----------------------------------------------------|
| `broadcaster/services/users.py`                       | Rewrite `import_from_xlsx`: tier-B detection, dept_loc tracking, return shape. Add `import_to_csv_errors()` helper. |
| `broadcaster/services/users.py`                       | `_normalize_phone` / `_validate_phone` / `_validate_email` unchanged. |
| `broadcaster/routes/admin_users.py`                   | Add `POST /api/users/upload-excel/errors.csv` route. Existing routes unchanged. |
| `broadcaster/templates/admin/users.html`              | Remove reload button. Replace flat inline banner content with a button that opens the modal. |
| `static/js/users.js`                                  | Replace xlsx-input handler logic per §4. Add `openImportErrorsModal`, `closeImportErrorsModal`, `humanizeError` extensions for the new reason codes. |
| `static/css/admin.css`                                | Tiny addition: `.modal-card .modal-body { max-height: 60vh; overflow: auto; }` (or wrap the table in a div with that style). |
| `tests/test_users_import.py` (likely new or merged)   | See §9. |
| `docs/superpowers/specs/2026-06-29-…design.md`       | Add a final note pointing to this spec as the implementation source for skip-report UI. |

### 9. Testing

| #  | Case                                                     | Expectation                                                                |
|----|----------------------------------------------------------|----------------------------------------------------------------------------|
| 1  | 3 clean rows, distinct phones+emails                     | inserted=3, errors=[], dept_loc_changed depends on inputs                 |
| 2  | Same phone twice in file                                 | Both rows error with `duplicate_phone_in_file`                             |
| 3  | Same email twice in file                                 | Both rows error with `duplicate_email_in_file`                             |
| 4  | Email matches existing DB user                           | Error `duplicate_email_in_db` (and row skipped)                            |
| 5  | Re-import existing export (upsert=true, all match)       | updated=N, skipped=0, no false errors, dept_loc_changed=False if unchanged |
| 6  | Bad phone format                                         | Error `invalid_phone_format`                                               |
| 7  | New dept value in imported row                           | `dept_location_changed=True`, `groups_created>0`                          |
| 8  | Update name+phone only, dept/loc unchanged               | `dept_location_changed=False`, `groups_created=0`                         |
| 9  | Empty file (no rows past header)                         | `{inserted:0, updated:0, skipped:0, errors:[], dept_loc_changed:false}`   |
| 10 | Replace mode (NOT in scope) — defer with explicit skip   | Test placeholder comment: "see 2026-06-29 spec, not implemented here"     |
| 11 | Errors CSV endpoint with empty errors array              | 400 `no_errors`                                                            |
| 12 | Errors CSV endpoint with 3 errors                        | 200 with text/csv body containing header + 3 rows, RFC-4180 quotes        |
| 13 | JS modal: open + close triggers `location.reload()`      | Verified by Playwright or manual smoke                                     |
| 14 | Manual reload button REMOVED from /admin/users DOM       | Assert button absent                                                        |

Playwright smoke (localhost:8123):

1. `/admin/users` → upload a known-clean file → green banner → page auto-reloads → new rows visible → Groups page shows new Dept/Loc.
2. Upload a file with 2 bad rows + 2 clean → modal opens → rows table shows 2 errors → Close → page reloads → only the 2 clean rows in the table.
3. Upload a 2nd file that has a phone from row 1 of the first upload (re-upload scenario) → updates silently, no false duplicates flagged.

### 10. Decision record

- **Modal vs inline disclosure (vs 2026-06-29's expandable banner)**: user explicitly chose modal for clear visibility. Inline banner is harder to read with 12+ rows.
- **Auto-reload after modal close, not immediate**: avoids "rows appear before user reads why some failed" UX bug.
- **Conditional rebuild, not unconditional**: pure name/phone updates shouldn't pay the DELETE+INSERT cycle cost on every auto-group. Asymmetric — cheap import may not need a cheap rebuild.
- **Existing `rebuild_auto_groups()` reused**: full wipe-and-rebuild of *auto* groups (manual groups untouched) is already correct semantics here. Per the user's 2026-06-29 decision, this function is already approved.
- **Errors CSV endpoint, not embedded in upload response**: keeps the upload response small for the common case (1 error or none). Errors CSV is opt-in.
- **Tier C kept (upsert=true silently updates phone conflicts)**: preserves the "re-import the export" workflow which the user has not asked to break.
- **Renames 2026-06-29 `invalid_phone`/`invalid_email` → `*_format`**: consistency with the new sibling codes; the front-end `humanizeError` map is updated together.
- **Backdrop/Escape does NOT reload**: explicit opt-out for users who want to read the modal without immediately refreshing. Mirrors standard dialog UX.

## Out of scope (deferred)

- Replace mode (destructive delete, admin-protected) — see 2026-06-29 §1.
- Inline-disclosure UX (replaced by modal in this spec).
- CSV download happens here; .xlsx skipped-report endpoint in 2026-06-29 §4 deferred.
- Import history persistence.
- Per-import navigation page.
- Column-letter error mapping.
- Apply to other imports.
- Soft-deactivate.

## Acceptance criteria

- [ ] `import_from_xlsx` returns errors with `field` + `value` for every reason code (including the 3 new ones).
- [ ] In-file dups (`phone` or `email`) both surface as errors with the new reason codes; no DB write happens for either row.
- [ ] DB-side email conflicts surface as `duplicate_email_in_db`; no DB write happens for the row.
- [ ] DB-side phone conflicts with `upsert=true` still silently update (regression-guard).
- [ ] After a clean import with `inserted+updated>0`, `location.reload()` fires automatically with no button click.
- [ ] After an import with `skipped>0`, a modal opens showing Row/Field/Value/Reason; backing-click and Escape hide the modal WITHOUT reload; Close triggers reload.
- [ ] The manual "Reload to see new users" button is gone from the DOM.
- [ ] When the import changes the dept/location set (case+whitespace-insensitive, non-empty), `rebuild_auto_groups()` runs and `groups_created>0`; when it doesn't, the function does NOT run.
- [ ] `POST /api/users/upload-excel/errors.csv` returns a parseable RFC-4180 CSV with the right columns, or 400 when errors is empty.
- [ ] All pre-existing tests still pass. New tests in §9 added and green.
- [ ] Manual smoke against `/home/asim/Desktop/user.xlsx` + a synthetic "with-errors" variant confirms both paths.
