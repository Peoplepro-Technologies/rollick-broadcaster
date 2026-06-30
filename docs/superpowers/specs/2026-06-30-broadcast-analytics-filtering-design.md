# Broadcast List Page — Analytics + Filtering

**Status:** Proposed
**Date:** 2026-06-30
**Scope:** `/admin/broadcasts` (HTML page only). The JSON `/api/broadcasts` endpoint gets filter-param parity so client tools / future JS use the same vocabulary.

## Goals

Admins running Rollick Broadcaster today can see *a list* of broadcasts but can't answer:
1. "How many broadcasts went out for *Promo on WhatsApp* vs *General on Email* — sent vs pending?"
2. "Show me everything I scheduled for last quarter."

This spec adds:
- A **counter grid** above the table, one card per `(category, delivery_channel)` pair that has at least one broadcast, showing prominently **Sent** and **Pending** with finer status counts collapsed behind `<details>`.
- A **filter bar** (form GET) with category, channel, and date range. Filtering the table also reconciles the counters — both come from the same filtered query so they cannot disagree.
- **Shareable URLs** — the filter state lives in the query string, not in JS state.

## Non-Goals

- No new JS framework, no HTMX, no live wire-up. SSR + form GET only.
- No new categories table / taxonomy. Categories remain free-text values entered on the compose page (`Admin → Broadcasts → New`). The category `<select>` is auto-populated from distinct values already in the DB.
- No charts, no graphs, no time-series. Counts only.
- No per-row click-through from card to filtered list. The filter form already does this.
- No new admin route / new page. The existing `/admin/broadcasts` is augmented in place.
- No export of the counter grid (CSV/PDF). Out of scope for v1.

## Definitions

These terms are anchored here so the rest of the spec is unambiguous:

| Term | Meaning |
|---|---|
| **Sent** | `broadcasts.status = 'sent'` exactly. |
| **Pending** | `broadcasts.status IN ('draft', 'queued')`. A draft is unscheduled-and-unstarted; a queued has a future scheduled time and is in APScheduler. |
| **Other** | `status IN ('sending', 'partial', 'failed', 'cancelled')`. Not counted as Sent or Pending. Shown collapsed behind `<details>` so the headline number on each card stays honest. |
| **Category** | Free-text `broadcasts.category` string, defaults to `'General'`. Values are not normalized. (Today there is no separate `categories` table and we are not introducing one.) |
| **Channel** | `broadcasts.delivery_channel` ∈ `{'whatsapp', 'email', 'both'}`. Already validated to this set at create time. |
| **Date filter date** | `broadcasts.scheduled_at` (not `created_at`, not `sent_at`). Rationale: admins think about broadcasts by *when they were scheduled to go out*. |
| **Draft pass-through** | If a date filter is active, `scheduled_at IS NULL` is included in addition to `scheduled_at BETWEEN date_from AND date_to` — so unscheduled drafts remain visible. |

## Architecture

### Files touched

1. **`broadcaster/services/broadcasts.py`** — extended.
   - Extend `list_broadcasts(...)` kwargs with `category`, `channel`, `date_from`, `date_to`. Same WHERE-clause shape it already has.
   - Add new function `count_broadcasts_by_category_channel(...)` with the same filter kwargs; returns `list[dict]` rows of `{category, channel, sent, pending, sending, partial, failed, cancelled, total}`.
   - Add new function `distinct_categories()` returning `list[str]` for the filter `<select>`.
   - Add private helper `_broadcast_filters_where(filters)` returning `(where_sql, params)` so list and count use the *exact same* filter (single source of truth).

2. **`app.py`** — `/admin/broadcasts` page route extended.
   - Read the four query params (empty string = not applied).
   - Validate via one helper. Bad inputs produce a flash, not a 400/500.
   - Call `list_broadcasts(**filters)` and `count_broadcasts_by_category_channel(**filters)` and `distinct_categories()`. Channel select is static `[whatsapp, email, both]`.
   - Render `admin/broadcasts_list.html` with `{broadcasts, counts, applied, category_options, channel_options, flash}`.

3. **`broadcaster/templates/admin/broadcasts_list.html`** — extended.
   - Above the existing table card: add the filter `<form>` and the counter-grid `<div>`.
   - Existing table block unchanged.

4. **`tests/test_broadcasts_page.py`** — new file.
   - 11 tests, listed in §Testing below.

5. **`tests/test_broadcasts_api.py`** (or wherever the existing `/api/broadcasts` tests live) — small addition.
   - One or two parity tests confirming `/api/broadcasts` accepts the same filter params and the SQL helper produces equivalent results for the API vs the page.

6. **`static/css/admin.css`** — small additions.
   - `.counter-grid` (flex / grid wrap), `.counter-card` (rounded box matching existing card style), `.counter-sent` (green), `.counter-pending` (orange), `.counter-other` (muted).
   - `.filter-row` (flex row, matches the existing table-card actions row).

No other files change. No DB migrations (the schema is already sufficient — we have `category`, `delivery_channel`, `status`, `scheduled_at`, `created_at`).

## SQL

### Single source of filter truth

`_broadcast_filters_where(filters: dict) -> tuple[str, list]` returns a string of `AND`-joined clauses + the bound params list. Both `list_broadcasts()` and `count_broadcasts_by_category_channel()` call it.

### Extending `list_broadcasts`

```sql
SELECT b.id, b.title, b.category, b.delivery_channel, b.status, b.scheduled_at,
       b.sent_at, b.created_at, b.generate_links,
       (SELECT COUNT(*) FROM broadcast_links  WHERE broadcast_id = b.id) AS link_count,
       (SELECT COUNT(*) FROM broadcast_targets WHERE broadcast_id = b.id) AS target_count
FROM   broadcasts b
WHERE  <filters>
ORDER  BY b.id DESC;
```

The new filters, appended to the existing `where` list:

| Filter | Clause | Param |
|---|---|---|
| `category` (non-empty) | `b.category = ?` | `category` |
| `channel`  (non-empty) | `b.delivery_channel = ?` | `channel` |
| `date_from` (non-empty) AND `date_to` (non-empty) | `(b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)` | `date_from 00:00:00`, `date_to 23:59:59` (UTC) |
| Only one of date_from / date_to set | Treated as no filter (flash) | — |

(If we wanted to support open-ended ranges — `?date_from=2026-01-01` with no `date_to` — that's a follow-up. v1 requires both bounds or neither.)

### New count query

```sql
SELECT  category, delivery_channel AS channel,
        SUM(CASE WHEN status = 'sent'                          THEN 1 ELSE 0 END) AS sent,
        SUM(CASE WHEN status IN ('draft','queued')             THEN 1 ELSE 0 END) AS pending,
        SUM(CASE WHEN status = 'sending'                       THEN 1 ELSE 0 END) AS sending,
        SUM(CASE WHEN status = 'partial'                       THEN 1 ELSE 0 END) AS partial,
        SUM(CASE WHEN status = 'failed'                        THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN status = 'cancelled'                     THEN 1 ELSE 0 END) AS cancelled,
        COUNT(*)                                               AS total
FROM    broadcasts
WHERE   <filters>
GROUP BY category, delivery_channel
HAVING  COUNT(*) > 0
ORDER BY category, delivery_channel;
```

`HAVING COUNT(*) > 0` is technically redundant (every group has count ≥ 1) but documents intent and protects against a future edit that swaps in a no-row inner table.

## UI

### Filter bar

Renders as a form above the counter grid, with `method="get" action="/admin/broadcasts"`. Each control is a real form control so it works without JS:

```
[ Category ▾ ]  [ Channel ▾ ]  [ From 📅 ] [ To 📅 ]   [ Apply ]  [ Clear ]
```

- Clear is `<a href="/admin/broadcasts">Clear</a>`, not a button — gives the canonical "all" URL.
- The page route echoes back the applied filter values so the form's `<option>`/`<input>` reflect current state after submit.

### Counter grid

A flex container with one card per row from the count query. Card layout:

```
┌─────────────────────────────────────┐
│ Promo · whatsapp                    │  ← head
│ Sent 42  ·  Pending 5               │  ← prominent (green / orange)
│ ▸ Other 3                           │  ← collapsed <details>; rendered only when non-zero
└─────────────────────────────────────┘
```

Card is hidden from layout when its row has `total = 0` (the SQL `HAVING` already enforces this).

If the result list is empty:

```
No broadcasts match these filters.
```

## Error Handling

Single helper `_validate_filters(query_params) -> tuple[dict, str | None]`:

| Input | Behaviour |
|---|---|
| `category` not in `distinct_categories()` | Ignored (treated as no filter). Doesn't error — could happen if URL is hand-edited. |
| `channel` not in `{whatsapp, email, both}` | Ignored. |
| `date_from > date_to` | Both ignored, flash `"date_from (YYYY-MM-DD) cannot be after date_to."`, both controls reset to empty. |
| `date_from` set, `date_to` absent (or vice versa) | Both ignored, flash `"Pick both dates or leave both empty."`. |
| `date_from` / `date_to` not parseable as ISO date | Treated as absent. |
| Missing or empty values | No-op. |

The page must render in all cases. No 4xx, no 5xx from filter inputs.

## Data Flow

```
user hits /admin/broadcasts?category=Promo&date_from=...&date_to=...
        │
        ▼
app.py admin_broadcasts_page():
  filters, flash = _validate_filters(request.query_params)
  with get_db() as conn:                              # one connection
    broadcasts = bc_svc.list_broadcasts(**filters)
    counts     = bc_svc.count_broadcasts_by_category_channel(**filters)
    categories = bc_svc.distinct_categories()
  render broadcast_list.html with {broadcasts, counts,
            applied=filters, category_options=categories,
            channel_options=static, flash=flash}
```

Two SQL queries per request (list + count). On the existing data shape and the existing SQLite indices (`idx_broadcasts_status`, `idx_broadcasts_sched`), this is sub-millisecond. No new index needed; an index on `category` *could* help if the table grows past ~10k rows and distinct-category filtering becomes hot — out of scope for v1, easy follow-up.

## Testing

`tests/test_broadcasts_page.py` uses the existing async-httpx client against an in-memory SQLite (per `tests/conftest.py` fixtures). 12 tests:

| # | Test | Asserts |
|---|---|---|
| 1 | `test_counts_split_sent_pending_correctly` | Same cat/ch, mixed statuses → card has `sent=1`, `pending=2`, `total=3`; other counts zero. |
| 2 | `test_counts_group_by_category_and_channel` | Two different cat/ch combos → two cards, totals add up to overall total. |
| 3 | `test_counts_excludes_partial_and_failed_from_pending` | partial=1, failed=1, sent=1, draft=1 → card has `sent=1`, `pending=1`, `partial=1`, `failed=1`. |
| 4 | `test_filter_category_narrows_table_and_counts` | URL `?category=Promo` → table only Promo; only Promo cards in counts. |
| 5 | `test_filter_channel_narrows_table_and_counts` | URL `?channel=email` → same symmetry. |
| 6 | `test_filter_date_range_applies_to_scheduled_at_with_null_passthrough` | 2 in-range + 1 out-of-range + 1 unscheduled (NULL sched) → all 4 visible. |
| 7 | `test_filter_date_range_excludes_out_of_range_when_no_null_present` | Only scheduled broadcasts, mix of in/out → only in-range visible. |
| 8 | `test_invalid_date_range_flashes_and_keeps_table` | `?date_from=2026-06-30&date_to=2026-06-01` → 200 OK, flash message visible, table unfiltered (full list). |
| 9 | `test_counts_and_table_agree` | For each category, sum of `total` across its cards equals number of table rows in that category. |
| 10 | `test_filter_form_preserves_values` | Submit `?category=Promo` → page's `<select name=category>` contains `selected` on the Promo option. |
| 11 | `test_clear_link_resets_filters` | Render page with filters set; the Clear link's `href` is exactly `/admin/broadcasts` (no query string). |
| 12 | `test_single_date_bound_flashes_and_keeps_table` | `?date_from=2026-06-30` (no `date_to`) → 200 OK, flash message, table unfiltered. |

Plus a single parity test in the API suite: `test_api_broadcasts_accepts_same_filter_kwargs` — confirms `/api/broadcasts?category=Promo&...` returns the same rows as `/admin/broadcasts?category=Promo&...` (both go through `_broadcast_filters_where`).

## Open Decisions None

All UX questions resolved in pre-implementation brainstorming. No items left deferred.

## Migration / Rollout

Pure-additive. No DB schema change. No URL collision (existing URL gains optional query params). Existing clients of `/api/broadcasts` keep working (the new params are optional). Roll out by deploying — no data backfill needed.

## Follow-Ups (Not in this Spec)

- Open-ended date range (`?date_from` without `?date_to`).
- Index on `broadcasts(category)` if list page slows at scale.
- Clickable counter cards that drop a filter pill ("Promo · whatsapp") into the form.
- Per-status "Other" breakdown exposed as a separate column toggle instead of `<details>`.
- Export counter grid as CSV.
