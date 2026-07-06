"""End-to-end tests for /admin/broadcasts counters + filters.

These 12 tests correspond one-to-one with the Testing section of
docs/superpowers/specs/2026-06-30-broadcast-analytics-filtering-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from broadcaster.services import broadcasts as bc_svc

from tests.test_broadcasts import (
    _login, _make_users, _set_broadcast_status,
)


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


# Test 1: counts split sent/pending correctly


async def test_counts_split_sent_pending_correctly(authed_client):
    a, = await _make_users(authed_client, ("PageU", "7400000001", "", ""))
    bids = []
    for status in ("sent", "queued", "draft"):
        r = await authed_client.post("/api/broadcasts", json={
            "title": f"T-{status}", "category": "Promo", "delivery_channel": "whatsapp",
            "user_ids": [a], "mode": "draft",
        })
        assert r.status_code == 200, r.text
        bids.append(r.json()["id"])
    _set_broadcast_status(bids[0], "sent")
    _set_broadcast_status(bids[1], "queued")
    _set_broadcast_status(bids[2], "draft")
    counts = bc_svc.count_broadcasts_by_category_channel()
    promo_wa = [r for r in counts if (r["category"], r["channel"]) == ("Promo", "whatsapp")][0]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 2  # queued + draft
    assert promo_wa["total"] == 3


# Test 2: counts group by category and channel


async def test_counts_group_by_category_and_channel(authed_client):
    a, = await _make_users(authed_client, ("PageU2", "7400000002", "", ""))
    # Two broadcasts in (Promo, whatsapp), one in (Promo, email) → 2 cards
    for title, cat, ch in [("P-W-1", "Promo", "whatsapp"),
                            ("P-W-2", "Promo", "whatsapp"),
                            ("P-E-1", "Promo", "email")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    rows = bc_svc.count_broadcasts_by_category_channel()
    keys = {(r["category"], r["channel"]) for r in rows}
    assert keys == {("Promo", "whatsapp"), ("Promo", "email")}
    promo_wa = next(r for r in rows if r["channel"] == "whatsapp")
    assert promo_wa["total"] == 2
    promo_em = next(r for r in rows if r["channel"] == "email")
    assert promo_em["total"] == 1


# Test 3: counts exclude partial/failed from pending


async def test_counts_excludes_partial_failed_from_pending(authed_client):
    a, = await _make_users(authed_client, ("PageU3", "7400000003", "", ""))
    for title, status in [("Sent-1", "sent"), ("Part-1", "partial"), ("Fail-1", "failed")]:
        r = await authed_client.post("/api/broadcasts", json={
            "title": title, "category": "Promo", "delivery_channel": "whatsapp",
            "user_ids": [a], "mode": "draft",
        })
        assert r.status_code == 200
        _set_broadcast_status(r.json()["id"], status)
    rows = bc_svc.count_broadcasts_by_category_channel(category="Promo", channel="whatsapp")
    promo_wa = rows[0]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 0  # partial / failed live in "other", not pending
    assert promo_wa["partial"] == 1
    assert promo_wa["failed"] == 1


# Test 4: filter category narrows table AND counts


async def test_filter_category_narrows_table_and_counts(authed_client):
    a, = await _make_users(authed_client, ("PageU4", "7400000004", "", ""))
    for title, cat, ch in [("A-Promo", "Promo", "whatsapp"),
                            ("A-General", "General", "email")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    listed = bc_svc.list_broadcasts(category="Promo")
    counts = bc_svc.count_broadcasts_by_category_channel(category="Promo")
    listed_cats = {b["category"] for b in listed}
    assert listed_cats == {"Promo"}
    count_cats = {(r["category"], r["channel"]) for r in counts}
    assert count_cats == {("Promo", "whatsapp")}


# Test 5: filter channel narrows table AND counts


async def test_filter_channel_narrows_table_and_counts(authed_client):
    a, = await _make_users(authed_client, ("PageU5", "7400000005", "", ""))
    for title, cat, ch in [("A-Promo-W", "Promo", "whatsapp"),
                            ("A-Promo-E", "Promo", "email"),
                            ("A-Gen-W", "General", "whatsapp")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    listed = bc_svc.list_broadcasts(channel="email")
    counts = bc_svc.count_broadcasts_by_category_channel(channel="email")
    listed_ch = {b["delivery_channel"] for b in listed}
    assert listed_ch == {"email"}
    count_ch = {r["channel"] for r in counts}
    assert count_ch == {"email"}


# Test 6: filter date_range applies to scheduled_at with NULL pass-through


async def test_filter_date_range_with_null_passthrough(authed_client):
    a, = await _make_users(authed_client, ("PageU6", "7400000006", "", ""))
    # 2 in-range + 1 out-of-range + 1 unscheduled → all 4 should be visible
    in_range_1_resp = await authed_client.post("/api/broadcasts", json={
        "title": "IR1", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    })
    assert in_range_1_resp.status_code == 200, in_range_1_resp.text
    _set_broadcast_status(in_range_1_resp.json()["id"], "queued")

    in_range_2_resp = await authed_client.post("/api/broadcasts", json={
        "title": "IR2", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
    })
    assert in_range_2_resp.status_code == 200, in_range_2_resp.text
    _set_broadcast_status(in_range_2_resp.json()["id"], "queued")

    oor_resp = await authed_client.post("/api/broadcasts", json={
        "title": "OOR", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
    })
    assert oor_resp.status_code == 200, oor_resp.text

    null_resp = await authed_client.post("/api/broadcasts", json={
        "title": "NullDraft", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",  # scheduled_at NULL
    })
    assert null_resp.status_code == 200, null_resp.text

    today = datetime.now(timezone.utc).date()
    listed = bc_svc.list_broadcasts(
        date_from=(today + timedelta(days=4)).isoformat(),
        date_to=(today + timedelta(days=12)).isoformat(),
    )
    titles = {b["title"] for b in listed}
    # IR1 (today+5), IR2 (today+10), NullDraft (NULL pass-through)
    assert "IR1" in titles
    assert "IR2" in titles
    assert "NullDraft" in titles
    assert "OOR" not in titles


# Test 7: filter date_range excludes out-of-range when no NULL present


async def test_filter_date_range_excludes_out_of_range(authed_client):
    a, = await _make_users(authed_client, ("PageU7", "7400000007", "", ""))
    in_range_resp = await authed_client.post("/api/broadcasts", json={
        "title": "IR", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    })
    assert in_range_resp.status_code == 200, in_range_resp.text
    oor_resp = await authed_client.post("/api/broadcasts", json={
        "title": "OOR", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
    })
    assert oor_resp.status_code == 200, oor_resp.text

    today = datetime.now(timezone.utc).date()
    listed = bc_svc.list_broadcasts(
        date_from=(today + timedelta(days=4)).isoformat(),
        date_to=(today + timedelta(days=12)).isoformat(),
    )
    titles = {b["title"] for b in listed}
    assert "IR" in titles
    assert "OOR" not in titles


# Test 8: invalid date range flashes and keeps table


async def test_invalid_date_range_flashes_and_keeps_table(authed_client):
    a, = await _make_users(authed_client, ("PageU8", "7400000008", "", ""))
    await authed_client.post("/api/broadcasts", json={
        "title": "BadDateTest", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    r = await authed_client.get(
        "/admin/broadcasts?date_from=2026-06-30&date_to=2026-06-01"
    )
    assert r.status_code == 200
    assert "date_from" in r.text
    assert "BadDateTest" in r.text  # full table still rendered


# Test 9: counts and table agree


async def test_counts_and_table_agree(authed_client):
    a, = await _make_users(authed_client, ("PageU9", "7400000009", "", ""))
    # 2 in Promo/whatsapp + 1 in Promo/email + 1 in General/whatsapp → 3 cards
    for title, cat, ch in [("P-W-a", "Promo", "whatsapp"),
                            ("P-W-b", "Promo", "whatsapp"),
                            ("P-E", "Promo", "email"),
                            ("G-W", "General", "whatsapp")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    # Total across all cards = total table rows
    rows = bc_svc.count_broadcasts_by_category_channel()
    total = sum(r["total"] for r in rows)
    listed = bc_svc.list_broadcasts()
    assert total == len(listed)
    # Per-category check
    promo_count_total = sum(r["total"] for r in rows if r["category"] == "Promo")
    promo_listed = [b for b in listed if b["category"] == "Promo"]
    assert promo_count_total == len(promo_listed) == 3


# Test 10: filter form preserves values


async def test_filter_form_preserves_values(authed_client):
    a, = await _make_users(authed_client, ("PageU10", "7400000010", "", ""))
    # Need at least one Promo broadcast so it appears in the <select>.
    await authed_client.post("/api/broadcasts", json={
        "title": "T-Promo", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    r = await authed_client.get("/admin/broadcasts?category=Promo")
    assert r.status_code == 200
    # The <option value="Promo">...</option> with the `selected` attribute
    assert 'value="Promo" selected' in r.text


# Test 11: clear link is bare URL


async def test_clear_link_is_bare_url(authed_client):
    r = await authed_client.get("/admin/broadcasts?category=Promo")
    assert r.status_code == 200
    # Clear link points to the bare URL (no query string). Class may
    # or may not be rendered depending on template — match the URL part.
    assert 'href="/admin/broadcasts"' in r.text
    assert '>Clear</a>' in r.text


# Test 12: single date bound flashes and keeps table


async def test_single_date_bound_flashes_and_keeps_table(authed_client):
    a, = await _make_users(authed_client, ("PageU12", "7400000012", "", ""))
    await authed_client.post("/api/broadcasts", json={
        "title": "SingleDateTest", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    r = await authed_client.get("/admin/broadcasts?date_from=2026-06-30")
    assert r.status_code == 200
    assert "both" in r.text.lower()
    assert "SingleDateTest" in r.text  # full table still rendered


# Test 13: q filter narrows table and counts to title match


async def test_search_q_narrows_table_and_counts(authed_client):
    a, = await _make_users(authed_client, ("PageU13", "7400000013", "", ""))
    for title, cat, ch in [("Diwali promo blast", "Promotions", "email"),
                            ("Holi greetings",       "Promotions", "whatsapp"),
                            ("Internal update",      "General",    "email")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    listed = bc_svc.list_broadcasts(q="diwali")
    titles = {b["title"] for b in listed}
    assert titles == {"Diwali promo blast"}

    listed2 = bc_svc.list_broadcasts(q="GREET")  # case-insensitive via LIKE
    assert {b["title"] for b in listed2} == {"Holi greetings"}

    # Page renders the matching row + the search input retains the value.
    r = await authed_client.get("/admin/broadcasts?q=diwali")
    assert r.status_code == 200
    assert "Diwali promo blast" in r.text
    assert "Holi greetings" not in r.text
    assert 'value="diwali"' in r.text  # search input preserved

    # Active-filter chip appears with the search term and an × link.
    assert 'search: <b>diwali</b>' in r.text


# Test 14: q combined with category filter narrows further


async def test_search_q_combines_with_category(authed_client):
    a, = await _make_users(authed_client, ("PageU14", "7400000014", "", ""))
    for title, cat in [("alpha news", "Promotions"),
                        ("alpha memo", "General"),
                        ("beta news",  "Promotions")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": "email",
            "user_ids": [a], "mode": "draft",
        })
    listed = bc_svc.list_broadcasts(q="alpha", category="Promotions")
    assert {b["title"] for b in listed} == {"alpha news"}


# ── /admin/broadcasts/new — search boxes for groups + users ──────
# The compose form renders a `<input type="search" class="filter-input">`
# above each of the two multi-select fieldsets. The vanilla-JS handler
# (inline in broadcast_compose.html) filters the visible `<label>` rows
# by case-insensitive substring against a pre-rendered
# `data-filter-text` attribute on each row. We test the HTML contract
# here (the data attributes are present and correct); a browser test
# would be needed to exercise the JS.

async def test_compose_form_renders_search_inputs_for_groups_and_users(authed_client):
    """The compose page must expose one search input per multi-select
    list, each linked to its target list by a stable id, and each
    filterable row must carry the searchable text in `data-filter-text`
    so the JS can hide non-matches without re-rendering. The live
    "X of Y" count + the "no matches" placeholder are also part of
    the contract — they make the search's effect visible."""
    from broadcaster.services import groups as groups_svc
    g1 = groups_svc.create_manual_group("Engineering", "manual", "")
    g2 = groups_svc.create_manual_group("Marketing", "manual", "")
    u1, u2 = await _make_users(authed_client, ("Alice Kumar", "7411111111", "", ""),
                                            ("Bob Smith",   "7422222222", "", ""))

    r = await authed_client.get("/admin/broadcasts/new")
    assert r.status_code == 200

    # Both search inputs are present and wired to the right list.
    assert 'data-filter-target="groups-list"' in r.text
    assert 'data-filter-target="users-list"' in r.text
    assert 'id="groups-list"' in r.text
    assert 'id="users-list"' in r.text

    # Every list row carries the searchable text. The group label
    # includes the member count in `data-filter-text` too, so typing
    # "(3)" would also narrow the list.
    for needle in ("engineering", "marketing", "alice kumar", "7411111111", "bob smith"):
        assert needle in r.text.lower(), f"missing {needle!r} in compose form"

    # The "no matches" placeholder exists but is hidden by default;
    # the JS toggles it on when the search has zero matches.
    assert 'data-filter-empty' in r.text
    # Two HTML elements (one per list) + one JS selector reference.
    assert r.text.count('data-filter-empty') >= 2

    # Live count is rendered for each list ("N of N" before any search).
    assert 'data-filter-count-for="groups-list"' in r.text
    assert 'data-filter-count-for="users-list"' in r.text
    assert '2 of 2' in r.text  # 2 groups, 2 users
    assert '0 of 0' not in r.text  # never rendered with zero total

    # The selection values are still wired correctly — search must not
    # break the form submission contract.
    assert f'value="{g1["id"]}"' in r.text
    assert f'value="{g2["id"]}"' in r.text
    assert f'value="{u1}"' in r.text
    assert f'value="{u2}"' in r.text
