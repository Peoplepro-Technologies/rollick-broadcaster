# Users Import — Skip Report — Design Spec

**Date:** 2026-06-29
**Status:** Draft (pending user review)
**Scope:** Augment `/admin/users` Excel-upload result with full visibility into skipped rows.

## Problem

The Excel import at `/admin/users` returns a three-count summary (`+X added, ~Y updated, !Z skipped`) plus — when there are skipped rows — at most the first three inline (`row N (reason); +N more`). Beyond that, the user has no way to know:

- **Which rows** were skipped (the offset within the spreadsheet, not the row ID).
- **Why** each one was skipped (a machine-readable code, not an explanation).
- **What the offending data was** (so they can fix it in their spreadsheet).

In practice the user uploads a 200-row list, sees `!17 skipped`, and has no next step besides opening the spreadsheet and guessing. The data needed to help them is already partially captured in the service response (`errors: [{row, reason}]`) — it's just not surfaced.

Real-world signal: a sample file at `/home/asim/Desktop/user.xlsx` (6 rows, all phones either duplicate or already in DB) currently produces `skipped == 0` with `upsert=true`. The skip path is only reachable today with `upsert=false` OR malformed rows — so the UI gap is real but rarely exercised.

## Goals & non-goals

**Goals**

- After every Excel import, the admin sees exactly which rows were skipped, why, and what the bad data was.
- The fix workflow is local: the admin opens the expandable skip table, reads the offending values, edits the source spreadsheet, and re-uploads.
- A `.xlsx` of just the skipped rows is one click away — same column layout as the import template so the user can drop straight back into Excel.
- No database changes, no new persistence, no new page — pure enhancement of the existing import flow.

**Non-goals (this iteration)**

- Persisting import history (each import is ephemeral — losing it on reload is acceptable).
- A dedicated `/admin/users/imports/{id}` report page.
- Surfacing skipped details for other imports (e.g. broadcasts). Add later if needed.
- Highlighting failed rows back to column letters (e.g. "Column B has a bad value") — easier UX, but skip for v1.
- Auto-correcting data on the admin's behalf (e.g. silently normalizing bad phones). The current `_normalize_phone` already does best-effort cleaning — that's separate from reporting.

## Design

### 1. Response shape (service)

`import_from_xlsx` currently returns `{inserted, updated, skipped, errors: [{row, reason}]}`. Each error gains two new fields. The response JSON becomes:

```json
{
  "inserted": 2,
  "updated": 4,
  "skipped": 3,
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

- `original` is the full row as `_row_to_dict` parsed it (exactly the dict that would have been saved). Powers the .xlsx download — no server-side state stored.
- `offending` is the minimal subset the admin needs to identify what's wrong (compact).
- `reason_human` is pre-translated so the frontend doesn't need a lookup table.

### 2. Reason → offending map (server-side, single helper)

| `reason`                  | `offending`                                       | `reason_human`                          |
|---------------------------|---------------------------------------------------|-----------------------------------------|
| `name_or_phone_missing`   | `{name, phone}` only the empty ones               | "Name and phone are required."          |
| `invalid_phone`           | `{phone: raw_str}`                                | "Phone must be 10 digits."              |
| `invalid_email`           | `{email: raw_str}`                                | "Email format is invalid."              |
| `phone_taken`             | `{phone: norm10}`                                 | "A user with that phone already exists." |
| `db_error: <msg>`         | `{db_error: msg}` (truncated to 80 chars)         | "Database error: <msg>."                |

`invalid_phone` carries the raw string the admin typed, so they can match it against their spreadsheet (where it may have been a string or a number stored as int — we use `str()` to preserve what's on disk).

### 3. New endpoint (download)

```
POST /api/users/upload-excel/skipped-report
Content-Type: application/json
Body: { "errors": [ … ] }     ← identical to what /upload-excel returned
Response:
  Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
  Content-Disposition: attachment; filename="users_skipped_YYYYMMDD-HHMMSS.xlsx"
```

The handler is a pure function over the request body — it does **not** re-read any stored state. It re-builds a one-sheet workbook with columns:

| Row | Reason | Reason (human) | Name | Phone | Email | Department | Location | is_active |

Header row includes bold + a second comment-row explaining the format ("Fix the rows in your spreadsheet and re-upload via /admin/users.").

If `errors` is empty or missing, the endpoint returns `400 {"detail": "no_skipped_rows"}`.

### 4. Frontend (`/admin/users` page + `static/js/users.js`)

**Banner** stays as-is: `✓ Import complete — +2 added, ~4 updated, !12 skipped.`

**Skip disclosure** appears below the banner only when `skipped > 0`:

```
[Show 12 skipped rows ▾]   [Download as .xlsx ⤓]
```

- Click the toggle → expands a `<table>` populated from `response.errors`.
- Click again → collapses.
- The **Download** button is always visible (active regardless of toggle state).
- If `skipped == 0`, neither the toggle nor the download button appears.

**Inline table columns:**

| Row | Reason | Details |
|-----|--------|---------|
| 7   | invalid_phone | `phone: "12345" (must be 10 digits)` |
| 12  | invalid_email | `email: "foo@"` |
| 15  | name_or_phone_missing | `name is empty` |
| 22  | phone_taken | `phone: "9876543210"` |

The `Details` cell is a one-liner composed in JS from `offending` + `reason_human`. When multiple fields are offending (`name_or_phone_missing`), all of them are listed.

**Wire behaviour** (single-flight, no double-uploads):

- Toggle is a real `<button aria-expanded>` so screen readers work.
- Clicking the toggle rapidly is harmless (it's a DOM toggle, no fetch).
- The Download button: while a fetch is in flight, shows `Generating…` and disables itself. A second click before the first resolves is ignored (in-flight flag, no queue).
- If the download errors (network, 500), show an inline `✗ Couldn't generate the report — try again.` below the button. The table stays visible.

No new icons, no new CSS classes beyond what exists (`.btn`, `.btn.small`, `.card`, `.data-table`).

### 5. Accessibility & no regressions

- The toggle is keyboard-reachable (real `<button>`).
- The download button honors its `disabled` state (visible + functional).
- All existing functionality (`+X/~Y/!Z` summary, reload button after success, error banner on failure) is preserved. **Zero behaviour changes** for uploads that produce 0 skipped rows.

### 6. Testing

**Automated (`tests/test_users.py`):**

- `import_from_xlsx` enriched error tests — one per reason code:
  - `errors[0].offending == {phone: "<raw>"}`, `errors[0].original` populated
  - `errors[0].reason_human` matches expected string
  - For `invalid_phone`, `offending.phone` is the raw string the user uploaded (preserved as `str()` so a number stored as int in the sheet still shows the original)
  - For `name_or_phone_missing`, only the empty field(s) appear in `offending` — populated fields are omitted
- `/api/users/upload-excel/skipped-report`:
  - 200 + parseable `.xlsx` on a real request
  - Header row matches expected layout
  - Body rows have `original` data spread across the right columns
  - `400 no_skipped_rows` when `errors` is empty
- **End-to-end** with a synthetic in-memory xlsx (3 rows: 1 valid + 1 `invalid_phone` + 1 missing name) → response has `inserted=1, updated=0, skipped=2` and the errors list matches each reason type.

**Manual verification — using `/home/asim/Desktop/user.xlsx`:**

1. Upload the file as-is with the default `upsert=true`. Expect `+0 added, ~6 updated, !0 skipped`. The skip disclosure does **not** appear. Banner unchanged from today. Smoke test passes.
2. Take a copy of that file (`/home/asim/Desktop/user-with-bad-rows.xlsx`) and inject three deliberate mistakes:
   - Row 7 (new): name `"Bogus"`, phone `"12345"` → `invalid_phone`.
   - Row 8 (new): phone only, empty name → `name_or_phone_missing`.
   - Row 9 (new): name `"Demo"`, phone `"1111111111"`, email `"notanemail"` → `invalid_email`.
3. Upload the variant with `upsert=true`. Expect `+3 added, ~0 updated, !3 skipped`.
4. Confirm in the UI:
   - `Show 3 skipped rows ▾` toggle appears.
   - Expanding shows three rows with row #, reason, and the offending value(s).
   - `Download as .xlsx ⤓` produces a file with three rows + correct headers + the bad data.
5. Open the downloaded .xlsx in Excel, fix `12345` → a real 10-digit number, save. Re-upload. Expect `!0 skipped` for the previously-bad rows specifically.

This mirrors the user's real fix-and-reupload workflow end-to-end.

### 7. Decision record

- **`upsert=false` flows**: when the admin actively chose "insert-only, fail on duplicates", the skip path shows `phone_taken` for every duplicate. That's the loudest version of the old message — the disclosure lands naturally.
- **`upsert=true` flows**: skips only come from genuinely-bad rows (bad phone format, bad email, missing required fields). For the user's current sample file this is 0; with the bad-row variant it'll be 3. Either way the disclosure only renders when there's something to show.
- **Why no in-DB import history**: YAGNI. Re-uploads are safe (idempotent by phone when upserted). If a user needs to recover the last import's report, the `.xlsx` they downloaded is the audit trail.
- **Why enrich the API response instead of fetching errors separately**: the alternative was a server-side "last 10 errors" buffer keyed on session. That's per-user state, more code, harder to test, and the data is already in the response. The download endpoint is a pure function over the response body — same model as the live endpoint, just a different content type.

## Out of scope (deferred)

- Import history persistence
- Per-import navigation (`/admin/users/imports/{id}`)
- Column-letter error mapping (`Column B has bad phone`)
- Apply this surface to other imports (broadcasts, comment moderation)
- Bulk auto-correct on import (e.g. silently strip dashes from phones)

## Acceptance criteria

- [ ] `import_from_xlsx` response errors carry `offending`, `original`, `reason_human` for all five reason codes.
- [ ] New endpoint `POST /api/users/upload-excel/skipped-report` returns a parseable `.xlsx` mirroring the response shape.
- [ ] `/admin/users` page shows an inline expandable table when `skipped > 0` with one row per skip, columns Row/Reason/Details.
- [ ] "Download as .xlsx" button is always visible when `skipped > 0`, regardless of toggle state.
- [ ] Uploads with `skipped == 0` behave identically to today (no extra UI elements rendered).
- [ ] All existing `tests/test_users.py` tests still pass.
- [ ] New tests cover all 5 reason codes + end-to-end route test + the new endpoint.
- [ ] Manual verification with both `/home/asim/Desktop/user.xlsx` (happy path) and a deliberate-mistake variant (skip-report path) — both confirmed by the user.
