# Users Import — Skip Report + Replace Mode — Design Spec

**Date:** 2026-06-29
**Status:** Draft (pending user review)
**Scope:** Augment `/admin/users` Excel-upload with (a) full visibility into skipped rows and a downloadable .xlsx, and (b) a destructive "Replace existing users" mode that re-syncs the DB to the file.

## Problem

The Excel import at `/admin/users` returns a three-count summary (`+X added, ~Y updated, !Z skipped`) plus — when there are skipped rows — at most the first three inline (`row N (reason); +N more`). Beyond that, the user has no way to know:

- **Which rows** were skipped (the offset within the spreadsheet, not the row ID).
- **Why** each one was skipped (a machine-readable code, not an explanation).
- **What the offending data was** (so they can fix it in their spreadsheet).

In practice the user uploads a 200-row list, sees `!17 skipped`, and has no next step besides opening the spreadsheet and guessing. The data needed to help them is already partially captured in the service response (`errors: [{row, reason}]`) — it's just not surfaced.

Real-world signal: `/home/asim/Desktop/user.xlsx` (6 rows, all phones either duplicate or already in DB) currently produces `+0/~6/!0` with `upsert=true`. The skip path is only reachable today with `upsert=false` OR malformed rows — so the UI gap is real but rarely exercised.

A second pain point: the spreadsheet is the **source of truth** for the user's roster. Today there is no way to express "treat this file as the authoritative list" — uploading again leaves any user not in the file untouched. The user has asked for replace semantics on every upload, with safeguards.

## Goals & non-goals

**Goals**

- After every Excel import, the admin sees exactly which rows were skipped, why, and what the bad data was.
- The fix workflow is local: open the expandable skip table, read the offending values, fix in the source spreadsheet, and re-upload.
- A `.xlsx` of just the skipped rows is one click away — same column layout as the import template so the user can drop back into Excel.
- A "Replace existing users" toggle on the upload form: when on, users in the DB whose phone is NOT in the new file are deleted (admin user is preserved). Off by default — current additive behavior remains the safe default.
- No database schema changes; no new persistence; no new page.

**Non-goals (this iteration)**

- Persisting import history (each import is ephemeral — losing it on reload is acceptable).
- A dedicated `/admin/users/imports/{id}` report page.
- Surfacing skipped details for other imports (e.g. broadcasts). Add later if needed.
- Highlighting failed rows back to column letters (`"Column B has a bad value"`).
- Bulk auto-correct on import (the current `_normalize_phone` already does best-effort cleaning — separate from reporting).
- Soft-deactivate "removed" users under replace mode (the user picked **hard** delete; we honor that).
- Deleting cascade rows in `broadcast_user_links` (those keep pointing at the deleted users — broadcasting/history stays intact even after replace).

## Design

### 1. Replace mode semantics

**Default** (toggle off): current behavior — additive upsert. Rows not in the file are untouched.

**Replace** (toggle on): the set of phones in the DB after the import equals the set of phones in the file ∪ {admin phone}. Implemented as a per-row diff inside one transaction:
- **Insert** any row from the file whose phone isn't in the DB yet.
- **Update** any row already in the DB whose phone is in the file (refresh name/email/department/location/is_active; preserve `id` and `created_at`).
- **Skip** any row from the file whose data fails validation (the skip-report UI covers this; see §4).
- **Delete** any row in the DB whose phone is NOT in the file AND is NOT the admin user. The admin user (typically the first admin created in the system; identified by `users.is_admin = 1` if the column exists, or by `id = 1` as a fallback identifier — see Implementation notes) is **always preserved** even if not in the file.

Why per-row diff vs `TRUNCATE; INSERT`:
- Per-row preserves `users.id` and `users.created_at` for users present in the new file. Broadcast history (`broadcast_user_links`) references user IDs and would otherwise become stale.
- Per-row runs as one transaction. The DB never shows a half-replaced state.

**Hard-delete vs soft-delete** — user confirmed **hard DELETE** under replace mode. The reasoning: their roster is the spreadsheet; anything not in it shouldn't exist. Soft-deactivate is a v2 if it turns out they need reversibility.

### 2. API contract — `POST /api/users/upload-excel`

Query params:

| Param    | Type | Default | Notes |
|----------|------|---------|-------|
| `upsert` | bool | `true`  | If `false`, duplicate phones skip with `phone_taken` instead of update. Always ignored under replace mode (insert-only semantics). |
| `replace`| bool | `false` | If `true`, the existing user list is replaced with the file contents (admin preserved). See §1. |

Response:

```json
{
  "inserted": 2,
  "updated": 4,
  "skipped": 3,
  "deleted": 412,
  "replace": false,
  "errors": [
    {
      "row": 7,
      "reason": "invalid_phone",
      "reason_human": "Phone must be 10 digits.",
      "offending": { "phone": "12345" },
      "original": {
        "name": "Asha",
        "phone": 12345,
        "email": null,
        "department": null,
        "location": null,
        "is_active": true
      }
    }
  ]
}
```

- `replace` echoes the request param so the UI can confirm what mode actually ran.
- `deleted` is the count of users removed under replace mode (always `0` otherwise).
- Each `errors[]` entry carries `offending`, `original`, and `reason_human` for the skip table and the .xlsx download.

### 3. Reason → offending map (server-side, single helper)

| `reason`                  | `offending`                          | `reason_human`                          |
|---------------------------|--------------------------------------|-----------------------------------------|
| `name_or_phone_missing`   | `{name, phone}` only the empty ones  | "Name and phone are required."          |
| `invalid_phone`           | `{phone: raw_str}`                   | "Phone must be 10 digits."              |
| `invalid_email`           | `{email: raw_str}`                   | "Email format is invalid."              |
| `phone_taken`             | `{phone: norm10}`                    | "A user with that phone already exists." |
| `db_error: <msg>`         | `{db_error: msg}` (truncated 80 ch)  | "Database error: <msg>."                |

`invalid_phone` carries the raw string the admin typed (so they can match it against their spreadsheet, where it may be a string or a number stored as int — `str()` preserves what's on disk).

### 4. New endpoint — `POST /api/users/upload-excel/skipped-report`

```
Content-Type: application/json
Body: { "errors": [ … ] }     ← identical to what /upload-excel returned
Response:
  200 + Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
       Content-Disposition: attachment; filename="users_skipped_YYYYMMDD-HHMMSS.xlsx"
  400 {"detail": "no_skipped_rows"} when errors is empty or missing
```

Pure function over the request body — does NOT re-read stored state. Re-builds a one-sheet workbook with columns: `Row, Reason, Reason (human), Name, Phone, Email, Department, Location, is_active`. Header row is bold; a second explanatory row reads "Fix the rows in your spreadsheet and re-upload via /admin/users."

### 5. Frontend (`/admin/users` page + `static/js/users.js`)

The upload form grows a checkbox:

```
[ ] Replace existing users (admin is preserved)
```

Below it, an info-helper line:

> Enable to set the user list to exactly the rows in your file. Users in the DB whose phone isn't in the file will be deleted. The admin account is always kept.

When the box is checked AND the user clicks the Upload button, the JS shows a `confirm()` dialog before submitting:

> Replace mode — this will delete the existing user list and replace it with the file's contents. The admin account will be preserved.
> Continue?

(`n` cancels the upload entirely.)

Response rendering:

- **Banner** stays as today: `✓ Import complete — +2 added, ~4 updated, !12 skipped. (412 removed)`. The `(412 removed)` clause is appended only when `deleted > 0`.
- **Skip disclosure** below the banner — only when `skipped > 0`:
  ```
  [Show 12 skipped rows ▾]   [Download as .xlsx ⤓]
  ```
  Inline table: `Row | Reason | Details`. Details cell composes from `offending` + `reason_human`.
- **Existing**: full reload behavior preserved.
- **No new icons, no new CSS classes** beyond what already exists (`.btn`, `.btn.small`, `.card`, `.data-table`).
- **Race-safety**: Download button uses an `isFetching` flag so rapid double-clicks don't double-fire.

UX consistency: this checkbox sits next to the format-guide expander; the upload button is below it; the result banner is above the new disclosure.

### 6. Accessibility & no regressions

- Replace checkbox: real `<input type="checkbox">` with `<label>` so label-click toggles the box and screen readers announce state.
- `confirm()` dialog is keyboard-operable (native browser behavior).
- Skip toggle is a real `<button aria-expanded>`.
- All existing functionality (`+X/~Y/!Z` summary, reload button after success, error banner on failure) is preserved. **Zero behavior change** when:
  - Skip count is 0.
  - Replace toggle is unchecked.

### 7. Testing

**Automated (`tests/test_users.py`)** — split into two suites:

**Suite A — replace mode (new):**

- Replace with empty-ish file (header + 2 valid rows): only those 2 users exist after; all others gone. Admin still exists.
- Replace with file missing the admin's row: admin still exists, deleted count includes the others but NOT the admin.
- Replace when no file rows are valid (all bad data): only admin remains; all old users deleted; `deleted == (old_count - 1)` (admin excluded).
- Replace then replace again with the SAME file: row counts stabilize; no churn.
- Add-mode untouched: `replace=false` leaves untouched users in place.
- Replace preserves `id` and `created_at` for users whose phone was already in the file (helps `broadcast_user_links` integrity).
- Replace audit: response carries `deleted` and `replace: true`.
- Skipped rows under replace mode still produce the enriched `errors` list (the per-row diff path doesn't disable validation).

**Suite B — skip report (from previous spec):**

- Each reason code produces the right `offending` / `original` / `reason_human`.
- `/api/users/upload-excel/skipped-report` returns a parseable .xlsx with the expected columns.
- 400 `no_skipped_rows` when errors array is empty.
- End-to-end synthetic in-memory xlsx covering all 5 reason codes.

**Manual verification — `/home/asim/Desktop/user.xlsx`:**

1. **Happy path** — Upload as-is with replace off. Expect `+0 added, ~6 updated, !0 skipped`. Banner + skip-disclosure both behave as today.
2. **Skip-report path** — Inject 3 bad rows in a copy (e.g., `user-with-bad-rows.xlsx`):
   - Row 7: name `"Bogus"`, phone `"12345"` → `invalid_phone`.
   - Row 8: phone only, empty name → `name_or_phone_missing`.
   - Row 9: name `"Demo"`, phone `"1111111111"`, email `"notanemail"` → `invalid_email`.
   
   Upload with replace off. Expect `+3 added, ~0 updated, !3 skipped`. Skip table shows the three rows with reasons and offending values. Download as .xlsx → file opens in Excel with the same three rows. Fix `12345` → a real 10-digit number → re-upload → `!0 skipped` for that row.
3. **Replace path** — Upload `/home/asim/Desktop/user.xlsx` with replace on. Confirm dialog appears: "Replace mode — this will delete the existing user list…". Confirm. Expect:
   - Admin preserved.
   - User table contains exactly the 6 rows from the file (re-creating them if they were renamed).
   - Banner shows `+0/~6/!0 (N removed)`.
   - Skip-disclosure hidden.
4. **Replace safety** — Upload the same file again with replace on. Expect: same 6 users; `deleted=0`; counts clean.
5. **Skipped rows under replace** — Upload `user-with-bad-rows.xlsx` with replace on. Expect: bad rows skipped (and shown in disclosure), bad rows NOT inserted, good rows inserted (or updated), all old unrelated rows deleted.

### 8. Decision record

- **Per-row diff over TRUNCATE + INSERT**: preserves `users.id`, which is referenced by `broadcast_user_links`. Truncate-then-insert would orphan every broadcast's audience mapping and complicate the dashboard's "Views by recipient" analytics.
- **Admin protected via the lowest-id user**: the bootstrap-admin creates the first user with `id=1`. Rather than add a `is_admin` column (schema change), protect the user with the lowest `id` (or the user whose password authenticates the admin — picked up by inspecting the active session row). Implementation note in §1.
- **Hard delete on replace (vs soft deactivate)**: user explicitly chose this. Tradeoff: irreversibility vs simplicity. The confirm-modal is the only guardrail; the admin-protected exemption keeps the admin logged in.
- **`upsert` param retained under non-replace mode**: existing API contract. Under replace mode it's ignored (every non-skipped row is either inserted or updated by the diff logic; phone_taken is impossible because the diff already deleted conflicting users).
- **`replace` param defaults to false**: matches the safe-by-default posture for an admin-only surface. Anyone who wants destructive behavior must check the box AND confirm the dialog.
- **No new CSS classes, no new icons**: keep visual diff minimal; reuse `.btn`, `.card`, `.data-table`. The checkbox uses native `<input type="checkbox">` styling.

## Out of scope (deferred)

- Import history persistence
- Per-import navigation (`/admin/users/imports/{id}`)
- Column-letter error mapping
- Apply to other imports (broadcasts)
- Soft-deactivate under replace (v2)
- Pre-flight count via separate endpoint (the `confirm()` shows a generic message; if needed, an extra GET can be added later for an exact count)

## Acceptance criteria

**Replace mode**

- [ ] `POST /api/users/upload-excel?replace=true` hard-deletes every user whose phone is not in the file AND not the admin-protected user, in a single transaction.
- [ ] Admin-protected user (lowest-id) survives any replace, even when absent from the uploaded file.
- [ ] Response carries `deleted` count and echoes `replace: true`.
- [ ] Skip-report UI still works under replace mode (bad rows still surface in the disclosure).
- [ ] Default (`replace=false`) leaves existing behavior unchanged.

**Skip report**

- [ ] `import_from_xlsx` response errors carry `offending`, `original`, `reason_human` for all five reason codes.
- [ ] New `POST /api/users/upload-excel/skipped-report` returns a parseable `.xlsx`.
- [ ] Inline expandable table on `/admin/users` when `skipped > 0`.
- [ ] Download button visible when `skipped > 0`, regardless of toggle state.
- [ ] Uploads with `skipped == 0` behave identically to today.

**UX**

- [ ] Replace checkbox on `/admin/users` is off by default.
- [ ] Native `confirm()` dialog blocks submission; cancel prevents the upload.
- [ ] `(N removed)` is appended to the banner only when `deleted > 0`.

**Tests**

- [ ] New tests in `tests/test_users.py`: 8 replace-mode cases + 5 skip-report cases + 1 skipped-rows-under-replace + 1 skipped-report endpoint round-trip.
- [ ] All pre-existing tests still pass.
- [ ] Manual smoke with both the real file and the deliberate-mistake variant — both paths confirmed by the user.
