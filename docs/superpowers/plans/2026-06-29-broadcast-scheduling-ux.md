# Broadcast Scheduling UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken `prompt()`-based scheduling flow with a date/time picker on the compose form (with presets, Asia/Kolkata timezone, and friendly list-view formatting) and one-click rescheduling from the detail page.

**Architecture:** Backend stays thin — `services/broadcasts._validate_future_iso` is a single shared helper used by `create_broadcast` and `schedule_broadcast`. The HTTP layer accepts an optional `scheduled_at` and an optional `mode` on `POST /api/broadcasts`. Frontend gets a new ES module `static/js/schedule.js` and a Jinja macro `templates/admin/_schedule_picker.html` reused on compose and detail; the same module also formats list-view times client-side. No DB migration.

**Tech Stack:** FastAPI + SQLite (existing), Jinja2 templates, vanilla ES module JS, native `<input type="datetime-local">`. No new deps.

---

## File Structure

**New files:**
- `broadcaster/templates/admin/_schedule_picker.html` — Jinja macro rendering the picker block; reused on compose + detail.
- `static/js/schedule.js` — ES module: `initSchedulePicker(rootEl, opts)`, `formatScheduledForList(iso, now)`, preset helpers.

**Modified files:**
- `broadcaster/services/broadcasts.py` — add `scheduled_at` and `mode` kwargs to `create_broadcast`; add private helper `_validate_future_iso`.
- `broadcaster/routes/admin_broadcasts.py` — pass new fields through.
- `broadcaster/templates/admin/broadcast_compose.html` — render picker block + dynamic submit button.
- `broadcaster/templates/admin/broadcast_detail.html` — replace `scheduleBroadcast` JS with picker flow; add reschedule action for queued.
- `broadcaster/templates/admin/broadcasts_list.html` — emit `data-scheduled-at` attribute; include list formatter bootstrap.
- `static/css/admin.css` — add `.when-block`, `.radio-card`, `.chip-row`, `.chip`, `.chip.active`, `.tz-hint`, `.field-warning`.
- `tests/test_broadcasts.py` — add 4 new tests; rename existing test file reference inside if needed.

**No new dependencies; no DB migration.**

---

## Task 1: Backend helper `_validate_future_iso` + tests

**Files:**
- Modify: `broadcaster/services/broadcasts.py` (top of file: imports + new helper)
- Modify: `tests/test_broadcasts.py` (add `TestValidateFutureIso` block near top)

- [ ] **Step 1: Write the failing test**

Append the following block near the top of `tests/test_broadcasts.py` (above the `# ── Create ──` marker):

```python
# ── _validate_future_iso ─────────────────────────────────────

from datetime import datetime, timezone, timedelta
from broadcaster.services import broadcasts as bc_svc


def test_validate_future_iso_returns_iso_for_future_utc():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    out = bc_svc._validate_future_iso(future)
    # round-trip parse
    parsed = datetime.fromisoformat(out)
    assert parsed > datetime.now(timezone.utc)


def test_validate_future_iso_rejects_past_with_400():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    import httpx
    with pytest.raises(httpx.HTTPStatusError) as exc:
        # service raises HTTPException; httpx would only catch via ASGI, so call directly
        bc_svc._validate_future_iso(past)
    # _validate_future_iso raises HTTPException — verify HTTPStatusError-ish via FastAPI
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc2:
        bc_svc._validate_future_iso(past)
    assert exc2.value.status_code == 400
    assert exc2.value.detail == "scheduled_at_in_past"


def test_validate_future_iso_rejects_naive_datetime():
    # naive datetimes are interpreted as UTC by _validate_future_iso, then compared
    past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        bc_svc._validate_future_iso(past_naive)
    assert exc.value.detail == "scheduled_at_in_past"


def test_validate_future_iso_rejects_garbage():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        bc_svc._validate_future_iso("not-a-date")
    assert exc.value.detail == "scheduled_at_invalid"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_broadcasts.py -k "validate_future_iso" -v`
Expected: 4 failures, all `AttributeError: module 'broadcaster.services.broadcasts' has no attribute '_validate_future_iso'`.

- [ ] **Step 3: Implement `_validate_future_iso`**

At the top of `broadcaster/services/broadcasts.py`, add the import (it's not present today):

```python
from datetime import datetime, timezone
from fastapi import HTTPException
```

(Note: `HTTPException` may already be imported — adjust to the existing line.)

Then add the helper **immediately after the imports**, before `def create_broadcast`:

```python
def _validate_future_iso(scheduled_at: str) -> str:
    """Parse an ISO datetime string, ensure it's in the future (UTC).

    Returns the normalised ISO string (with timezone). Raises HTTP 400 on
    invalid or past datetimes so client and server share one definition.
    """
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="scheduled_at_invalid")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="scheduled_at_in_past")
    return dt.isoformat()
```

Do **not** remove any existing imports — only add what's missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_broadcasts.py -k "validate_future_iso" -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/services/broadcasts.py tests/test_broadcasts.py
git commit -m "feat(scheduling): extract _validate_future_iso helper with shared client+server validation"
```

---

## Task 2: Backend — `create_broadcast` accepts `scheduled_at` and `mode`

**Files:**
- Modify: `broadcaster/services/broadcasts.py` (extend `create_broadcast` signature + behavior)
- Modify: `tests/test_broadcasts.py` (add 4 tests under `# ── Create ──`)

- [ ] **Step 1: Write the failing tests**

Insert these after the existing `test_create_broadcast_with_group` test in `tests/test_broadcasts.py`:

```python
async def test_create_with_future_scheduled_at_creates_queued(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000091", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Scheduled", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "schedule",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["scheduled_at"] is not None


async def test_create_with_past_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000092", "", ""))
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Past", "user_ids": [a],
        "scheduled_at": past_iso, "mode": "schedule",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "scheduled_at_in_past"


async def test_create_with_draft_mode_and_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000093", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Ambiguous", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "draft",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "ambiguous_schedule_payload"


async def test_create_with_garbage_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000094", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Garbage", "user_ids": [a],
        "scheduled_at": "not-a-date", "mode": "schedule",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "scheduled_at_invalid"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_broadcasts.py -k "future_scheduled_at or past_scheduled_at or draft_mode and scheduled_at or garbage_scheduled_at" -v`
Expected: 4 failures; statuses will be 200 not 400 because today the server ignores `scheduled_at`/`mode`.

- [ ] **Step 3: Extend `create_broadcast` signature + behavior**

In `broadcaster/services/broadcasts.py`, modify `create_broadcast`. Update signature to:

```python
def create_broadcast(
    title: str,
    category: str = "General",
    message_text: Optional[str] = None,
    content_id: Optional[int] = None,
    delivery_channel: str = "whatsapp",
    group_ids: Optional[Iterable[int]] = None,
    user_ids: Optional[Iterable[int]] = None,
    generate_links: bool = True,
    created_by: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    mode: str = "draft",  # "schedule" | "send_now" | "draft"
) -> dict:
```

Inside the function, immediately after the existing validation block (`if delivery_channel not in ...`), add:

```python
    # Validate ambiguous payloads before scheduling work
    if mode not in ("draft", "schedule", "send_now"):
        raise HTTPException(status_code=400, detail="invalid_mode")
    if mode == "draft" and scheduled_at:
        raise HTTPException(status_code=400, detail="ambiguous_schedule_payload")
    if mode == "send_now" and scheduled_at:
        raise HTTPException(status_code=400, detail="ambiguous_schedule_payload")

    initial_status = "draft"
    normalised_scheduled_at: Optional[str] = None
    if scheduled_at is not None:
        normalised_scheduled_at = _validate_future_iso(scheduled_at)
        initial_status = "queued"
    if mode == "send_now":
        initial_status = "queued"  # /send will fire immediately
        normalised_scheduled_at = normalised_scheduled_at or (
            datetime.now(timezone.utc) + timedelta(seconds=5)
        ).isoformat()
```

(Add `from datetime import timedelta` to the imports at the top if not already there.)

Then change the INSERT statement to use the dynamic status and scheduled_at:

```python
        cur = conn.execute(
            "INSERT INTO broadcasts (title, category, message_text, content_id, "
            "delivery_channel, generate_links, created_by, created_at, "
            "scheduled_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), category or "General", message_text, content_id,
             delivery_channel, 1 if generate_links else 0, created_by, now_str,
             normalised_scheduled_at, initial_status),
        )
```

After the link-generation block, before `return b`, add:

```python
    # Wire into the scheduler if queued
    if initial_status == "queued" and normalised_scheduled_at is not None:
        from broadcaster.services import scheduler as sched_svc
        sched_svc.schedule_broadcast(bid, normalised_scheduled_at)
```

(Leave the existing link_info block and `return b` unchanged.)

- [ ] **Step 4: Update the route to forward new fields**

In `broadcaster/routes/admin_broadcasts.py`, replace the body of `create_broadcast` with:

```python
@router.post("")
def create_broadcast(payload: dict, request_admin_id: int = Depends(require_admin)):
    from broadcaster.services import admin as admin_svc
    creator = admin_svc.find_by_id(request_admin_id)
    return bc_svc.create_broadcast(
        title=payload.get("title", ""),
        category=payload.get("category", "General"),
        message_text=payload.get("message_text"),
        content_id=payload.get("content_id"),
        delivery_channel=payload.get("delivery_channel", "whatsapp"),
        group_ids=payload.get("group_ids") or [],
        user_ids=payload.get("user_ids") or [],
        generate_links=bool(payload.get("generate_links", True)),
        created_by=creator["username"] if creator else None,
        scheduled_at=payload.get("scheduled_at"),
        mode=payload.get("mode", "draft"),
    )
```

- [ ] **Step 5: Run new tests to verify they pass**

Run: `pytest tests/test_broadcasts.py -k "future_scheduled_at or past_scheduled_at or draft_mode and scheduled_at or garbage_scheduled_at" -v`
Expected: 4 passed.

- [ ] **Step 6: Run the full broadcast test suite to verify no regression**

Run: `pytest tests/test_broadcasts.py tests/test_scheduler.py -v`
Expected: all previously passing tests still pass; new tests from Task 1 + Task 2 pass.

- [ ] **Step 7: Commit**

```bash
git add broadcaster/services/broadcasts.py broadcaster/routes/admin_broadcasts.py tests/test_broadcasts.py
git commit -m "feat(scheduling): create_broadcast accepts scheduled_at + mode; rejects ambiguous payloads"
```

---

## Task 3: Frontend — write `static/js/schedule.js`

**Files:**
- New: `static/js/schedule.js`

- [ ] **Step 1: Create the file with full content**

Create `static/js/schedule.js` with this content exactly (no abstractions beyond what's listed):

```javascript
// static/js/schedule.js
// ES module for broadcast scheduling UX.
// Depends on: native Intl, no other deps.

const DEFAULT_TZ = "Asia/Kolkata";

function nowInTz(tz) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find(p => p.type === t).value;
  return { year: get("year"), month: get("month"), day: get("day"),
           hour: get("hour"), minute: get("minute") };
}

// ── Presets ──────────────────────────────────────────────────

function presetIn15(now = new Date(), tz = DEFAULT_TZ) {
  const d = new Date(now.getTime() + 15 * 60 * 1000);
  return formatLocal(d, tz);
}

function presetIn1Hour(now = new Date(), tz = DEFAULT_TZ) {
  const d = new Date(now.getTime() + 60 * 60 * 1000);
  return formatLocal(d, tz);
}

function presetTomorrow9(now = new Date(), tz = DEFAULT_TZ) {
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const ymd = nowInTz(tz);
  // build at 09:00 in the target tz by constructing that wall-clock
  const d = new Date(`${ymd.year}-${ymd.month}-${ymd.day}T09:00:00`);
  d.setDate(d.getDate() + 1); // advance 1 calendar day from "today in tz"
  return formatLocal(d, tz);
}

function presetNextMonday9(now = new Date(), tz = DEFAULT_TZ) {
  // day 1 = Monday in JS
  const target = new Date(now);
  const day = target.getDay(); // 0..6, Sun..Sat
  const daysUntilMon = ((1 - day + 7) % 7) || 7;
  target.setDate(target.getDate() + daysUntilMon);
  target.setHours(9, 0, 0, 0);
  return formatLocal(target, tz);
}

// Round a Date up to next 5 min in the target tz.
function roundUpTo5(date) {
  const d = new Date(date);
  const m = d.getMinutes();
  const add = (5 - (m % 5)) % 5;
  d.setMinutes(m + add, 0, 0);
  if (add === 0) d.setMinutes(d.getMinutes() + 5);
  return d;
}

function isoToLocalInput(iso, tz) {
  // Convert ISO UTC → yyyy-MM-ddTHH:mm suitable for <input type=datetime-local>
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(d);
  const get = (t) => parts.find(p => p.type === t).value;
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

function formatLocal(date, tz) {
  // Return the value suitable for input[type=datetime-local]
  const pad = (n) => String(n).padStart(2, "0");
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const get = (t) => parts.find(p => p.type === t).value;
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

function localInputToIso(localStr) {
  // datetime-local string has no TZ → interpret as local browser time, then toISOString(UTC)
  const d = new Date(localStr);
  return d.toISOString();
}

// ── Picker controller ───────────────────────────────────────

const PRESETS = [
  { id: "in15",  label: "+15 min",      compute: (n, tz) => formatLocal(roundUpTo5(new Date(n.getTime() + 15*60*1000)), tz) },
  { id: "in1h",  label: "+1 hour",      compute: (n, tz) => formatLocal(roundUpTo5(new Date(n.getTime() + 60*60*1000)), tz) },
  { id: "tom9",  label: "Tomorrow 9am", compute: presetTomorrow9 },
  { id: "mon9",  label: "Next Mon 9am", compute: presetNextMonday9 },
];

export function initSchedulePicker(rootEl, opts = {}) {
  const tz = opts.tz || (Intl.DateTimeFormat().resolvedOptions().timeZone) || DEFAULT_TZ;
  const onChange = opts.onChange || (() => {});
  const initial = opts.initial || formatLocal(roundUpTo5(new Date()), tz);

  const radios    = rootEl.querySelectorAll('input[name="_schedule_mode"]');
  const chipRow   = rootEl.querySelector(".chip-row");
  const customIn  = rootEl.querySelector('input[type="datetime-local"]');
  const submitBtn = rootEl.querySelector('[data-submit-button]');
  const warningEl = rootEl.querySelector("[data-warning]");
  const tzHint    = rootEl.querySelector("[data-tz-hint]");

  customIn.value = initial;
  if (tzHint) {
    tzHint.textContent = tz === DEFAULT_TZ
      ? "Asia/Kolkata (IST, UTC+5:30)"
      : `Timezone: ${tz}`;
  }

  // Build preset chips
  chipRow.innerHTML = "";
  for (const p of PRESETS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.dataset.preset = p.id;
    btn.textContent = p.label;
    btn.addEventListener("click", () => {
      const v = p.compute(new Date(), tz);
      customIn.value = v;
      // Also select the schedule radio if not selected
      const scheduleRadio = rootEl.querySelector('input[name="_schedule_mode"][value="schedule"]');
      if (scheduleRadio && !scheduleRadio.checked) scheduleRadio.checked = true;
      onChange();
    });
    chipRow.appendChild(btn);
  }

  customIn.addEventListener("input", onChange);

  for (const r of radios) r.addEventListener("change", onChange);

  function selectedMode() {
    const r = rootEl.querySelector('input[name="_schedule_mode"]:checked');
    return r ? r.value : "schedule";
  }

  function getScheduledIso() {
    const m = selectedMode();
    if (m !== "schedule") return null;
    const v = customIn.value;
    if (!v) return null;
    return localInputToIso(v);
  }

  function validate() {
    const m = selectedMode();
    if (m !== "schedule") { warningEl.hidden = true; submitBtn.disabled = false; return true; }
    const v = customIn.value;
    if (!v) { warningEl.hidden = true; submitBtn.disabled = false; return false; }
    const iso = localInputToIso(v);
    const ms = new Date(iso).getTime() - Date.now();
    if (ms < 0) {
      warningEl.textContent = "Pick a time in the future.";
      warningEl.hidden = false;
      submitBtn.disabled = true;
      return false;
    }
    if (ms < 5 * 60 * 1000) {
      warningEl.textContent = `Fires in <5 min — Send now instead? `;
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = "Switch to Send now";
      link.onclick = (e) => { e.preventDefault();
        const r = rootEl.querySelector('input[name="_schedule_mode"][value="send_now"]');
        if (r) r.checked = true;
        onChange();
      };
      warningEl.appendChild(link);
      warningEl.hidden = false;
      submitBtn.disabled = false;
      return true;
    }
    warningEl.hidden = true;
    submitBtn.disabled = false;
    return true;
  }

  function updateSubmit() {
    const m = selectedMode();
    if (m === "send_now") {
      submitBtn.textContent = "Send to recipients";
    } else if (m === "draft") {
      submitBtn.textContent = "Save Draft";
    } else {
      const v = customIn.value;
      if (v) {
        const d = new Date(localInputToIso(v));
        const fmt = new Intl.DateTimeFormat("en-GB", {
          timeZone: tz, weekday: "short", day: "2-digit", month: "short",
          hour: "2-digit", minute: "2-digit", hour12: true, timeZoneName: "short",
        });
        submitBtn.textContent = `Schedule for ${fmt.format(d)}`;
      } else {
        submitBtn.textContent = "Schedule";
      }
    }
  }

  function refresh() {
    validate();
    updateSubmit();
  }

  onChange = (() => {
    const prev = onChange;
    return () => { prev(); refresh(); };
  })();
  refresh();

  return {
    tz, getMode: selectedMode, getScheduledIso, refresh,
  };
}

// ── List-view formatter ─────────────────────────────────────

const REL_THRESHOLDS = [
  { ms: 60 * 1000,           label: () => "in %d sec", div: 1000 },
  { ms: 60 * 60 * 1000,      label: () => "in %d min", div: 60 * 1000 },
  { ms: 24 * 60 * 60 * 1000, label: () => "in %d hr %smin", div: 60 * 60 * 1000 }, // simplified
];

export function formatScheduledForList(iso, now = new Date(), tz = DEFAULT_TZ) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (isNaN(dt)) return "—";
  const absDelta = Math.abs(dt - now);
  const future = dt > now;
  // Use Intl for absolute phrasing
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz, day: "2-digit", month: "short", hour: "2-digit",
    minute: "2-digit", hour12: true, timeZoneName: "short",
  });
  const abs = fmt.format(dt);
  if (absDelta < 30 * 1000) return future ? "Just now — firing any second" : "Just fired";
  if (future && absDelta < 60 * 1000) return `in ${Math.round(absDelta/1000)} sec`;
  if (future && absDelta < 60 * 60 * 1000) return `in ${Math.round(absDelta/(60*1000))} min`;
  if (future && absDelta < 24 * 60 * 60 * 1000) {
    const h = Math.floor(absDelta / (60*60*1000));
    const m = Math.round((absDelta % (60*60*1000)) / (60*1000));
    return `${abs} · in ${h}h ${m}m`;
  }
  if (future) return `${abs} · in ${Math.round(absDelta/(24*60*60*1000))}d`;
  return future ? `${abs}` : `Sent at ${abs}`;
}

export function applyListFormatter(rootEl = document) {
  rootEl.querySelectorAll("[data-scheduled-at]").forEach((td) => {
    const iso = td.dataset.scheduledAt;
    td.textContent = formatScheduledForList(iso);
  });
}
```

- [ ] **Step 2: Verify file parses (no syntax errors)**

Run: `node --check static/js/schedule.js && echo OK`
Expected: `OK`. (If `node` isn't installed, skip; the JS will be exercised by the browser in Task 7/8.)

If `node` is unavailable, run a simple Python check that the file exists and is non-empty:
Run: `test -s static/js/schedule.js && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add static/js/schedule.js
git commit -m "feat(scheduling): ES module with picker controller, presets, and list-view formatter"
```

---

## Task 4: Frontend — Jinja macro `_schedule_picker.html`

**Files:**
- New: `broadcaster/templates/admin/_schedule_picker.html`

- [ ] **Step 1: Create the macro file**

Create `broadcaster/templates/admin/_schedule_picker.html` with this exact content:

```jinja
{% macro schedule_picker(mode='schedule', initial='') %}
{# Render a "When to send?" picker block.
   mode: 'schedule' (default) | 'send_now' | 'draft'.
   initial: optional ISO UTC datetime to pre-fill the datetime-local input.
#}
<fieldset class="when-block">
  <legend>When to send?</legend>

  <label class="radio-card">
    <input type="radio" name="_schedule_mode" value="send_now" {% if mode == 'send_now' %}checked{% endif %}>
    <span>🚀 Send immediately</span>
  </label>

  <label class="radio-card">
    <input type="radio" name="_schedule_mode" value="schedule"
           {% if mode == 'schedule' or mode not in ['send_now','draft'] %}checked{% endif %}>
    <span>⏰ Schedule for later</span>
  </label>
  <div class="chip-row" data-chip-row></div>
  <div class="custom-row">
    <label>Or pick custom:
      <input type="datetime-local" name="_scheduled_at_local" value="{{ initial }}">
    </label>
    <small data-tz-hint class="tz-hint"></small>
  </div>
  <div data-warning class="field-warning" hidden></div>
  <input type="hidden" name="_scheduled_at" value="">
  <input type="hidden" name="mode" value="">

  <label class="radio-card">
    <input type="radio" name="_schedule_mode" value="draft" {% if mode == 'draft' %}checked{% endif %}>
    <span>📝 Save as draft</span>
  </label>
</fieldset>
{% endmacro %}
```

- [ ] **Step 2: Verify the macro loads (no syntax errors)**

Create a temporary smoke test in `tests/test_schedule_macro.py`:

```python
"""Smoke test for the schedule_picker Jinja macro."""
from broadcaster.app import app  # FastAPI app
from jinja2 import Environment, FileSystemLoader, select_autoescape

def test_schedule_picker_macro_renders():
    env = Environment(
        loader=FileSystemLoader(["broadcaster/templates"]),
        autoescape=select_autoescape(["html"]),
    )
    tpl_src = open("broadcaster/templates/admin/_schedule_picker.html").read()
    env2 = Environment(autoescape=select_autoescape(["html"]))
    env2.parse(tpl_src)  # raises if syntax invalid
    # Spot-check: macro produces expected radio names when rendered
    tmpl = env.from_string(
        '{% import "admin/_schedule_picker.html" as sp %}{{ sp.schedule_picker("schedule", "2026-12-31T09:00") }}'
    )
    out = tmpl.render()
    assert 'name="_schedule_mode"' in out
    assert 'name="_scheduled_at_local"' in out
    assert "Tomorrow 9am" not in out  # chips are added by JS, not macro
```

- [ ] **Step 3: Run the smoke test**

Run: `pytest tests/test_schedule_macro.py -v`
Expected: 1 passed.

- [ ] **Step 4: Delete the smoke test (it was just a syntax check; permanent tests live in Task 7/8)**

```bash
rm tests/test_schedule_macro.py
```

- [ ] **Step 5: Commit**

```bash
git add broadcaster/templates/admin/_schedule_picker.html
git commit -m "feat(scheduling): Jinja macro for the when-to-send picker block"
```

---

## Task 5: Frontend — integrate picker into `broadcast_compose.html`

**Files:**
- Modify: `broadcaster/templates/admin/broadcast_compose.html`
- Modify: `static/css/admin.css`
- Modify: `tests/test_broadcasts.py` (add a render-smoke test)

- [ ] **Step 1: Write a failing render test**

Append to `tests/test_broadcasts.py`:

```python
async def test_compose_form_renders_picker_block(client):
    """Unauthenticated GET — 302 redirect to login is fine; assert compose template
    (after login) includes the picker markup. Use the authed client to fetch /admin/broadcasts/new."""
    await _login(client)
    r = await client.get("/admin/broadcasts/new")
    assert r.status_code == 200
    html = r.text
    # The picker macro inserts these markers when imported by the compose template.
    assert 'name="_schedule_mode"' in html
    assert 'name="_scheduled_at_local"' in html
    assert 'class="when-block"' in html
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `pytest tests/test_broadcasts.py::test_compose_form_renders_picker_block -v`
Expected: FAIL — assertion error because the compose template today doesn't import the macro.

- [ ] **Step 3: Update `broadcast_compose.html` to import the macro and render the block**

Open `broadcaster/templates/admin/broadcast_compose.html`. At the top of the file (just under `{% block body %}` line) add:

```jinja
{% import "admin/_schedule_picker.html" as sp %}
```

Then find the `<form id="compose-form" …>` block. Just before the `<div id="compose-error" class="form-error" hidden></div>` line, insert:

```jinja
{{ sp.schedule_picker("schedule", "") }}
```

Then find the submit button:

```jinja
        <button type="submit" class="btn success">Save as Draft</button>
```

Replace with:

```jinja
        <button type="submit" class="btn success" data-submit-button>Schedule</button>
```

Finally, in the `<script>` block at the bottom, replace the entire body of `submitCompose` so it reads the new fields. Replace the entire `async function submitCompose(ev) { ... }` definition with:

```javascript
async function submitCompose(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  // Pull mode + scheduled_at from the picker
  const mode = fd.get("_schedule_mode") || "schedule";
  let scheduled_at = null;
  if (mode === "schedule") {
    const local = fd.get("_scheduled_at_local");
    if (local) {
      // datetime-local has no TZ → interpret as browser local, convert to UTC ISO
      scheduled_at = new Date(local).toISOString();
    }
  }
  const payload = {
    title: fd.get("title"),
    category: fd.get("category"),
    delivery_channel: fd.get("delivery_channel"),
    message_text: fd.get("message_text") || null,
    content_id: fd.get("content_id") || null,
    group_ids: fd.getAll("group_ids").map(Number),
    user_ids: fd.getAll("user_ids").map(Number),
    generate_links: !!fd.get("generate_links"),
    mode, scheduled_at,
  };
  const err = document.getElementById("compose-error");
  err.hidden = true;
  const r = await fetch("/api/broadcasts", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  if (r.ok) {
    const body = await r.json();
    location.href = `/admin/broadcasts/${body.id}`;
  } else {
    const b = await r.json().catch(() => ({}));
    err.textContent = b.detail || `Error ${r.status}`;
    err.hidden = false;
  }
}
</script>

<script type="module">
import { initSchedulePicker } from "/static/js/schedule.js";

const block = document.querySelector(".when-block");
if (block) initSchedulePicker(block, {});
</script>
```

- [ ] **Step 4: Add CSS for the picker block**

In `static/css/admin.css`, append the following at the very end of the file:

```css
/* ── Schedule picker ─────────────────────────────────────── */
.when-block { border: 1px solid var(--border, #e2e8f0); border-radius: 8px;
              padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
.when-block legend { font-weight: 600; padding: 0 8px; }
.radio-card    { display: flex; align-items: center; gap: 8px; padding: 6px 8px;
                 border-radius: 6px; cursor: pointer; }
.radio-card:hover { background: rgba(0,0,0,0.03); }
.radio-card input { margin: 0; }
.chip-row      { display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 28px; }
.chip          { background: #fff; border: 1px solid var(--primary, #ED0E6D);
                 color: var(--primary, #ED0E6D); border-radius: 999px; padding: 4px 12px;
                 cursor: pointer; font-size: 13px; }
.chip:hover, .chip.active { background: var(--primary, #ED0E6D); color: #fff; }
.custom-row    { display: flex; flex-direction: column; gap: 4px; padding: 4px 28px; }
.field-warning { padding: 4px 28px; color: #b45309; font-size: 13px; }
.tz-hint       { color: var(--muted, #64748b); font-size: 12px; }
```

(Adjust the `var(--primary, #ED0E6D)` fallback if your CSS has a different token name; cross-check with the top of `static/css/tokens.css` if uncertain.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_broadcasts.py::test_compose_form_renders_picker_block -v`
Expected: PASS.

- [ ] **Step 6: Run the full broadcast + scheduler tests for regression**

Run: `pytest tests/test_broadcasts.py tests/test_scheduler.py -v`
Expected: all passed (Tasks 1–2 tests included).

- [ ] **Step 7: Commit**

```bash
git add broadcaster/templates/admin/broadcast_compose.html static/css/admin.css tests/test_broadcasts.py
git commit -m "feat(scheduling): compose form renders picker block + dynamic submit label"
```

---

## Task 6: Frontend — integrate picker into `broadcast_detail.html` (replace `prompt()` + reschedule)

**Files:**
- Modify: `broadcaster/templates/admin/broadcast_detail.html`

- [ ] **Step 1: Modify the page header / button row**

In `broadcaster/templates/admin/broadcast_detail.html`, find the block:

```jinja
    <div>
      {% if broadcast.status == 'draft' %}
      <button class="btn small" onclick="scheduleBroadcast({{ broadcast.id }})">⏰ Schedule</button>
      <button class="btn success small" onclick="sendBroadcast({{ broadcast.id }})">🚀 Send Now</button>
      <button class="btn danger small" onclick="cancelBroadcast({{ broadcast.id }})">✕ Cancel</button>
      {% endif %}
      <button class="btn danger small" onclick="deleteBroadcast({{ broadcast.id }})">Delete</button>
    </div>
```

Replace with:

```jinja
    <div data-actions
         data-broadcast-id="{{ broadcast.id }}"
         data-broadcast-status="{{ broadcast.status }}"
         {% if broadcast.scheduled_at %}data-broadcast-scheduled-at="{{ broadcast.scheduled_at }}"{% endif %}></div>
```

(No `tojson` fallback needed — data-attributes are the contract. The Action buttons are populated by the script in Step 3.)

- [ ] **Step 2: Append a "When to send?" picker block + reschedule flow**

Just above the Analytics `<div class="card">…</div>` (i.e., after the existing `<div class="card">` block that displays the broadcast metadata), insert:

```jinja
{% if broadcast.status in ['draft','queued'] %}
  {% import "admin/_schedule_picker.html" as sp %}
  <div class="card">
    <div class="card-head"><h2>When to send</h2></div>
    {% if broadcast.status == 'queued' %}
      <p class="muted">Currently scheduled for <b>{{ broadcast.scheduled_at }}</b>. Pick a new time below and click Reschedule.</p>
    {% endif %}
    {{ sp.schedule_picker('schedule', broadcast.scheduled_at or '') }}
    <div class="form-actions">
      <button class="btn success" data-submit-button data-reschedule>Reschedule</button>
    </div>
  </div>
{% endif %}
```

- [ ] **Step 3: Replace the entire `<script>` block at the bottom**

Find:

```jinja
{% block scripts %}
<script>
async function revokeLink(...)
… existing functions …
</script>
{% endblock %}
```

Replace the whole `<script>…</script>` (everything inside `{% block scripts %}`) with:

```html
<script type="module">
import { initSchedulePicker } from "/static/js/schedule.js";

const actions = document.querySelector("[data-actions]");
const bid            = parseInt(actions.dataset.broadcastId, 10);
const status         = actions.dataset.broadcastStatus;
const scheduledAtIso = actions.dataset.broadcastScheduledAt || null;
if (status === "draft") {
  actions.innerHTML = `
    <button class="btn success small" data-act="send">🚀 Send Now</button>
    <button class="btn danger small" data-act="cancel">✕ Cancel</button>
    <button class="btn danger small" data-act="delete">Delete</button>
  `;
} else if (status === "queued") {
  actions.innerHTML = `
    <button class="btn danger small" data-act="cancel">✕ Cancel</button>
    <button class="btn danger small" data-act="delete">Delete</button>
  `;
} else if (status === "sent") {
  actions.innerHTML = `
    <a class="btn small secondary" href="#links">View Links</a>
    <button class="btn danger small" data-act="delete">Delete</button>
  `;
}

actions.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === "send" && !confirm("Send now? Recipients will get the message immediately.")) return;
  if (act === "cancel" && !confirm("Cancel this broadcast?")) return;
  if (act === "delete" && !confirm("Delete this broadcast and all its links/comments?")) return;
  const url = act === "send" ? `/api/broadcasts/${bid}/send`
            : act === "cancel" ? `/api/broadcasts/${bid}/cancel`
            : act === "delete" ? `/api/broadcasts/${bid}` : null;
  if (!url) return;
  const method = act === "delete" ? "DELETE" : "POST";
  const r = await fetch(url, { method });
  if (r.ok) location.reload();
  else { const b = await r.json().catch(() => ({})); alert(`Action failed: ${b.detail || r.status}`); }
});

const pickerBlock = document.querySelector(".when-block");
if (pickerBlock) {
  const ctl = initSchedulePicker(pickerBlock, { initial: scheduledAtIso || "" });
  document.querySelector("[data-reschedule]")?.addEventListener("click", async (ev) => {
    ev.preventDefault();
    const iso = ctl.getScheduledIso();
    const mode = ctl.getMode();
    if (mode === "schedule" && !iso) { alert("Pick a time first."); return; }
    if (mode === "send_now") {
      const r = await fetch(`/api/broadcasts/${bid}/send`, { method: "POST" });
      if (r.ok) location.reload();
      else { const b = await r.json().catch(()=>({})); alert(`Send failed: ${b.detail || r.status}`); }
      return;
    }
    if (mode === "draft") {
      // Move to draft via cancel + recreate? For v1 just alert.
      alert("To move to draft, click Cancel then reopen.");
      return;
    }
    const r = await fetch(`/api/broadcasts/${bid}/schedule`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ scheduled_at: iso }),
    });
    if (r.ok) location.reload();
    else { const b = await r.json().catch(()=>({})); alert(`Schedule failed: ${b.detail || r.status}`); }
  });
}

// Keep existing revokeLink helper
async function revokeLink(bid, lid) {
  if (!confirm("Revoke this link? The recipient will see an expired page.")) return;
  const r = await fetch(`/api/broadcasts/${bid}/links/${lid}/revoke`, { method: "POST" });
  if (r.ok) location.reload();
  else alert("Revoke failed: " + r.status);
}
window.revokeLink = revokeLink;
</script>
```

- [ ] **Step 4: Run the broadcast test suite**

Run: `pytest tests/test_broadcasts.py tests/test_scheduler.py -v`
Expected: all passed (existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add broadcaster/templates/admin/broadcast_detail.html
git commit -m "feat(scheduling): detail page picker + reschedule; replaces prompt() Schedule button"
```

---

## Task 7: Frontend — friendly list-view formatting in `broadcasts_list.html`

**Files:**
- Modify: `broadcaster/templates/admin/broadcasts_list.html`

- [ ] **Step 1: Replace the truncated ISO cell**

Find the `<td>` that displays the scheduled time:

```jinja
            <td style="font-size: 12px; color: var(--muted);">
              {{ b.scheduled_at[:16] if b.scheduled_at else '—' }}
            </td>
```

Replace with:

```jinja
            <td class="scheduled-cell" data-scheduled-at="{{ b.scheduled_at or '' }}">
              <span class="muted">…</span>
            </td>
```

- [ ] **Step 2: Add a list formatter bootstrap script**

At the end of the file, before `{% endblock %}`, append:

```jinja
{% block scripts %}
<script type="module">
  import { applyListFormatter } from "/static/js/schedule.js";
  applyListFormatter();
</script>
{% endblock %}
```

(If a `{% block scripts %}` is already present in this template, merge the script into the existing block instead of opening a new one.)

- [ ] **Step 3: Add a render smoke test**

Append to `tests/test_broadcasts.py`:

```python
async def test_list_emits_data_scheduled_at_marker(client):
    await _login(client)
    # Create one queued broadcast with a future time
    a, = await _make_users(client, ("A", "1000000095", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await client.post("/api/broadcasts", json={
        "title": "Listable", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "schedule",
    })
    r = await client.get("/admin/broadcasts")
    assert r.status_code == 200
    assert 'data-scheduled-at="' in r.text
```

- [ ] **Step 4: Run the new test**

Run: `pytest tests/test_broadcasts.py::test_list_emits_data_scheduled_at_marker -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/templates/admin/broadcasts_list.html tests/test_broadcasts.py
git commit -m "feat(scheduling): list view emits data-scheduled-at marker; JS formatter renders friendly time"
```

---

## Task 8: Final regression + manual smoke checklist

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -v`
Expected: all passed, including the new tests from Tasks 1, 2, 5, 7. No regressions.

- [ ] **Step 2: Run lint / format**

Run: `python -m pyflakes broadcaster tests || true; python -m black --check broadcaster tests || true`
Expected: clean (or only pre-existing warnings). If `black` would reformat, run `python -m black broadcaster tests` once and commit.

- [ ] **Step 3: Manual smoke checklist (browser via `/admin/broadcasts/new`)**

In order, verify:

- [ ] Compose page renders the picker; default is Schedule; submit button reads "Schedule for …".
- [ ] Click "+15 min" → input updates → submit label updates.
- [ ] Pick a past date → submit disabled + warning visible.
- [ ] Pick "Tomorrow 9am" preset, submit → redirect to detail page, status="queued", scheduled time matches.
- [ ] On detail page, picker visible with the scheduled time pre-filled; click Reschedule with a new time → status remains "queued" and time updates.
- [ ] On list page (`/admin/broadcasts`), the Scheduled cell shows `"Tomorrow 9:00 AM IST · in 18h"` (or similar friendly phrasing), not truncated ISO.
- [ ] Switch the radio to "Send immediately" → submit label changes to "Send to recipients"; submit fires `send_broadcast` and the row flips to `sent`.
- [ ] Switch the radio to "Save as draft" and submit → status="draft", no scheduler entry, no fire.

- [ ] **Step 4: Final commit (if Step 2 produced formatting changes)**

```bash
git add -u
git commit -m "chore: black formatting on broadcast scheduling changes"
```

(If `git diff` is empty after Step 2, skip this commit.)

- [ ] **Step 5: PR body checklist**

When opening the PR (or merging to main), include in the body:

- Summary: replaces `prompt()` Schedule button with a date/time picker; presets; Asia/Kolkata tz; friendly list view.
- Spec: `docs/superpowers/specs/2026-06-29-broadcast-scheduling-ux-design.md`
- Tests: tasks 1, 2, 5, 7 added tests; all 175+ previous tests still pass.
- Rollout: no DB migration, single PR, no infra changes.

---

## Acceptance criteria

- `pytest -v` exits 0.
- Compose form submit at "Schedule" produces `status='queued'` with a non-null `scheduled_at`.
- Reschedule from the detail page replaces the scheduler job without `Cancel` then `Schedule`.
- List view displays `<friendly phrasing> · <relative time>` rather than raw ISO.
- `prompt()` is no longer called anywhere in the admin templates (`grep prompt broadcaster/templates` returns nothing).
