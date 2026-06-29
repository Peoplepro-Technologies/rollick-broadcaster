# Broadcast Scheduling UX — Design

**Date:** 2026-06-29
**Status:** Approved
**Owner:** Rollick Dev (asym.b@peoplepro.co.in)

---

## Problem

Today the only way to schedule a broadcast for later is:

1. Compose the message on `/admin/broadcasts/new` (only button available: **Save as Draft**).
2. Navigate to the detail page.
3. Click **⏰ Schedule**, which fires `prompt('Schedule for (ISO 8601, e.g. 2026-12-31T10:00:00+00:00):')`.
4. Hand-type an ISO 8601 datetime string with timezone offset — or skip the step and only ever "Send Now".

The list page (`/admin/broadcasts`) shows the scheduled time truncated to `"2026-06-29T12:30"` which is opaque and timezone-implicit. Admins cannot tell at a glance when something will fire relative to *now*.

This is the worst UX in the admin. We are replacing it.

---

## Goal

Make scheduling broadcasts feel as easy as using a calendar app:

- Schedule happens on the **compose form** (one form, one submit), not after.
- The schedule input is a **date/time picker** with **preset chips**, not a prompt asking for ISO 8601.
- All times shown to admins are in their **local timezone** (Asia/Kolkata by default; auto-detected via browser).
- **Reschedule = change the time**, not cancel + recreate.
- The list view tells admins **when** each queued broadcast will fire relative to now.

---

## Non-goals (explicitly out of scope this round)

- ❌ Calendar view of all upcoming broadcasts.
- ❌ Recurring broadcasts ("every Monday at 9am").
- ❌ Per-recipient send-time offsets.
- ❌ Editing broadcast content after scheduled (existing behavior unchanged).
- ❌ Admin-side timezone override setting (uses browser auto-detect for v1).
- ❌ Drag-and-drop reschedule.

---

## UX

### 1. Compose form (`/admin/broadcasts/new`)

Add a `When to send?` block above the Cancel / Submit row, inside the same `<form>`:

```
┌─ When to send? ──────────────────────────────────────┐
│  ○  🚀  Send immediately                              │
│  ●  ⏰  Schedule for later                              │
│        [ +15 min ] [ +1 hr ] [ Tomorrow 9am ]           │
│        [ Next Mon 9am ]                                  │
│        Or pick custom: [ 30/06/2026  09:00 AM ]         │
│        Timezone: Asia/Kolkata (auto-detected)            │
│  ○  📝  Save as draft                                    │
└────────────────────────────────────────────────────────┘
                     [ Cancel ]  [ Schedule for Mon 30 Jun, 9:00 AM IST ]
```

- Default selected: **Schedule for later**, pre-filled with the current IST "now" rounded up to the next 5 minutes.
- Preset chips recolor on hover; clicking them overwrites the custom input and updates the submit button label.
- Submit button label changes dynamically to `"Schedule for <human-readable when>"`, `"Send to <N> recipients"`, or `"Save Draft"` based on selected mode.
- Validation:
  - **Past datetime** → inline error "Pick a time in the future" + disable submit.
  - **Today, within 5 min** → warning chip "Fires in <5 min — Send now instead?" with quick toggle to Send Now mode.
  - **TZ hint**: if browser-detected TZ ≠ Asia/Kolkata, show small `(showing in Asia/Kolkata — switch to <browser-tz>?)` link.

### 2. Detail page (`/admin/broadcasts/{id}`)

Replace the `prompt()`-driven Schedule button with the same picker panel inline. The button row collapses to one primary action whose label updates with the picker:

- When `status='draft'`: Submit button = "Schedule for <when>" / "Send to N recipients" / "Save Draft" depending on mode.
- When `status='queued'`: Picker is visible; selecting a new time and clicking **Reschedule** replaces the existing scheduler job silently (no Cancel needed). Cancel button still available for hard-stop.

When `status='sent'`: picker is hidden, button row collapses to "View Links" + "Delete".

### 3. List page (`/admin/broadcasts`)

The `Scheduled` column reformats:

| Old                      | New                                                               |
| ------------------------ | ----------------------------------------------------------------- |
| `2026-06-29T12:30`       | `Tomorrow 9:00 AM IST · in 18h`                                   |
| (queued, <1 min away)    | `Just now — firing any second`                                    |
| (queued, <60s away)      | `in 45 sec`                                                       |
| (sent)                   | `Sent at 9:00 AM IST yesterday`                                   |
| (draft / no scheduled)   | `—` (unchanged)                                                   |

Rendered client-side via a small JS formatter so admins see fresh "in 3h 14m" counts without a page refresh tick.

---

## Data flow

### Existing endpoints, no new routes

| Verb + Path                              | Change                                                                                                            |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `POST /api/broadcasts`                   | Accept optional `scheduled_at: str` (ISO, UTC) in JSON payload. If present + valid future → `status='queued'`, call `scheduler.schedule_broadcast(bid, when_iso)`. |
| `POST /api/broadcasts/{bid}/schedule`    | Already calls `scheduler.schedule_broadcast()` with `id=f"broadcast:{bid}"` + `replace_existing=True`. **No change.** Used for reschedule from detail page. |
| `POST /api/broadcasts/{bid}/cancel`      | Unchanged.                                                                                                        |
| `POST /api/broadcasts/{bid}/send`        | Unchanged.                                                                                                        |

Frontend always converts admin-local datetime to UTC ISO before posting via JS `Date` + ISO string math.

### Status machine (unchanged from today)

```
   draft ──► queued ──► sent
     │         │
     │         └──► cancelled
     └──► (delete)
```

Transitions:

- Compose with no `scheduled_at` → `draft`.
- Compose with `scheduled_at` → `queued`.
- Detail page Reschedule (queued): replace `scheduled_at` and the scheduler job; status stays `queued`.
- Detail page Send Now (draft or queued) → fire `send_broadcast` immediately → `sent`.
- Detail page Cancel (queued) → `cancelled`.
- Scheduler auto-fires at time → `sent`.

---

## Frontend architecture

### New files

- `static/js/schedule.js` — ES module exporting:
  - `initSchedulePicker(rootEl, { mode, onChange })`
  - `formatScheduledForList(iso, now)` — for list view relative-time rendering.
  - Preset calculations (`presetIn15`, `presetIn1Hour`, `presetTomorrow9`, `presetNextMonday9`).
- `templates/admin/_schedule_picker.html` — Jinja macro `{% macro schedule_picker(mode='schedule', initial='') %}` rendering the markup + chip styles. Imported by both compose and detail templates.

### Modified files

- `broadcaster/templates/admin/broadcast_compose.html` — add `<fieldset class="when-block">…</fieldset>` block above form-actions, switch submit button to dynamic label.
- `broadcaster/templates/admin/broadcasts_list.html` — add `data-scheduled-at="{{ b.scheduled_at }}"` to the cell; wire to `formatScheduledForList` on load.
- `broadcaster/templates/admin/broadcast_detail.html` — replace `scheduleBroadcast` JS with new picker flow; same picker reused via macro.
- `static/css/admin.css` — new classes: `.when-block`, `.radio-card`, `.chip-row`, `.chip`, `.chip.active`, `.tz-hint`, `.field-warning`.

### Timezone detection & display

- On page load: `const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata'`.
- Store in a hidden `<input name="_tz">` per form so server logs can record it (no functional dependency).
- Display format: `Asia/Kolkata (IST, UTC+5:30)` on first load. If detected TZ differs, show switcher link.

### Browser compatibility

- Native `<input type="datetime-local">` (supported in all evergreen browsers).
- No external JS dependencies. No build step change.

---

## Backend / service changes

### `services/broadcasts.create_broadcast(...)`

New optional kwarg `scheduled_at: str | None = None`. Behavior:

1. Validate `scheduled_at` parses via `datetime.fromisoformat(...)` and is timezone-aware.
2. If `scheduled_at <= now_utc`: raise `HTTPException(400, detail="scheduled_at_in_past")`.
3. Insert with `status='queued'` and `scheduled_at=scheduled_at` if provided; else `status='draft'` and `scheduled_at=None` (existing behavior).
4. If queued, call `scheduler.schedule_broadcast(bid, scheduled_at)` (the function already exists, just wire the call).

### `services/broadcasts.schedule_broadcast(bid, when_iso)` (no functional change)

Already validates past-time, returns `broadcast` dict. Tighten by importing the same past-check so client + server agree.

### `services/scheduler`

No change. `rehydrate_pending` already handles missed fire-times on restart and a 30-second tick covers mid-flight app downtime.

### `broadcaster/settings.py`

No change.

---

## Edge cases

| Case                                       | Behavior                                                                                                         |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| User picks a past datetime                 | Inline error, submit disabled.                                                                                   |
| Today, <5 min in future                    | Yellow warning chip + quick-switch link to "Send now" mode.                                                      |
| Browser TZ ≠ Asia/Kolkata                  | Small switcher link "Show in <browser-tz>" under the picker. Does not change the stored value, only display.    |
| App down at fire-time                      | Already handled by `rehydrate_pending` (every 30s + on startup); fires within 30s of restart.                   |
| User reschedules a queued broadcast        | Same picker form, `/schedule` endpoint with `replace_existing=True`. Status stays `queued`.                      |
| Two browsers schedule the same broadcast   | Last write wins; second client sees the updated time on next refresh. We accept this race for v1.                |
| Form submitted with mode=Draft but time set | Strip the time before insert; treat as draft. Add server-side guard to reject ambiguous payloads (HTTP 400).      |
| `_tz` field included                       | Server ignores; not part of the contract.                                                                        |
| DST transition in Asia/Kolkata             | No DST in IST; safe.                                                                                             |

---

## Testing

### Unit / integration

- `tests/test_broadcasts.py`:
  - `test_create_with_future_scheduled_at_creates_queued` — POST with `scheduled_at` in future → response has `status='queued'`, scheduler called.
  - `test_create_with_past_scheduled_at_returns_400` — POST with past time → HTTP 400 `scheduled_at_in_past`.
  - `test_create_without_scheduled_at_creates_draft` — unchanged behavior.
  - `test_reschedule_via_schedule_endpoint_replaces_scheduler_job` — call `/schedule` twice with different future times; assert only one APScheduler job exists for `broadcast:{bid}`.
- `tests/test_scheduler.py`:
  - `test_reschedule_replaces_existing_job_id` — already partially covered; tighten.

### UI / template

- `tests/test_broadcasts.py::test_compose_form_includes_picker_block` — HTML response from `GET /admin/broadcasts/new` contains `<input type="datetime-local">` and four preset chip buttons (`+15 min`, `+1 hour`, `Tomorrow 9am`, `Next Mon 9am`).
- `tests/test_broadcasts.py::test_detail_page_includes_picker_when_queued` — HTML response contains picker markup and existing submit button for queued broadcasts.

### Manual / live smoke (after deploy)

1. Login → New Broadcast → fill form → pick "Tomorrow 9am" preset → click Schedule.
2. Confirm broadcast detail page shows status `queued`, scheduled time matches tomorrow 9am IST.
3. Refresh list view → Scheduled column reads `"Tomorrow 9:00 AM IST · in <N>h"`.
4. Open detail page → click "Reschedule" → pick "+15 min" → confirm status remains `queued` and the displayed time updates.
5. Wait past fire-time → confirm `status='sent'` and list reads `"Sent at …"`.

---

## Rollout

- Single PR; targeted surface (compose form, detail page, list view, one template macro, one JS module, one service signature change).
- No DB migration (column already exists, `scheduled_at` and `status` already wired).
- Tests must remain green: existing 175+ tests + new tests (target +10 new).
- No settings UI change; no infra change.

---

## Open questions

_None at signoff. Revisit if admin feedback surfaces blockers._
