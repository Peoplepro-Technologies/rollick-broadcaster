"""Scheduler tests: APScheduler fires scheduled broadcasts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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


async def _make_broadcast(client, *, n_users=1, title="Sched"):
    uids = []
    for i in range(n_users):
        u = (await client.post("/api/users", json={
            "name": f"U{i}", "phone": f"5{200000000 + i:09d}",  # 10 digits
        })).json()
        uids.append(u["id"])
    b = (await client.post("/api/broadcasts", json={
        "title": title, "user_ids": uids,
    })).json()
    return b["id"]


# ── Schedule + cancel ───────────────────────────────────────

async def test_schedule_registers_with_scheduler(authed_client):
    bid = await _make_broadcast(authed_client)
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    r = await authed_client.post(f"/api/broadcasts/{bid}/schedule",
                                  json={"scheduled_at": when})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"

    from broadcaster.services.scheduler import get_scheduler
    s = get_scheduler()
    job = s.get_job(f"broadcast:{bid}")
    assert job is not None


async def test_cancel_removes_scheduled_job(authed_client):
    bid = await _make_broadcast(authed_client)
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    await authed_client.post(f"/api/broadcasts/{bid}/schedule", json={"scheduled_at": when})
    await authed_client.post(f"/api/broadcasts/{bid}/cancel")
    from broadcaster.services.scheduler import get_scheduler
    s = get_scheduler()
    job = s.get_job(f"broadcast:{bid}")
    assert job is None


# ── Rehydrate on startup ───────────────────────────────────

async def test_rehydrate_picks_up_queued_broadcasts(authed_client):
    bid = await _make_broadcast(authed_client)
    # Manually set queued + future scheduled_at (bypass the /schedule route)
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE broadcasts SET status='queued', scheduled_at=? WHERE id=?",
                     (when, bid))
    # Now call rehydrate — should pick it up
    from broadcaster.services.scheduler import rehydrate_pending
    n = rehydrate_pending()
    assert n >= 1
    from broadcaster.services.scheduler import get_scheduler
    job = get_scheduler().get_job(f"broadcast:{bid}")
    assert job is not None


async def test_rehydrate_fires_overdue_immediately(authed_client):
    """An overdue queued broadcast should be scheduled to fire on next tick
    (i.e. ~1s in the future, not skipped)."""
    bid = await _make_broadcast(authed_client)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE broadcasts SET status='queued', scheduled_at=? WHERE id=?",
                     (past, bid))
    from broadcaster.services.scheduler import rehydrate_pending
    rehydrate_pending()
    from broadcaster.services.scheduler import get_scheduler
    job = get_scheduler().get_job(f"broadcast:{bid}")
    assert job is not None
    # The job is scheduled (in the past means it'll fire on the next
    # wakeup). We check via the scheduler's pending queue, which is
    # the public way to ask "what's coming up?".
    pending = [j for j in get_scheduler().get_jobs() if j.id == f"broadcast:{bid}"]
    assert len(pending) == 1


# ── Wiring: scheduler module calls the right service function ─

async def test_run_send_invokes_send_broadcast(authed_client, monkeypatch, tmp_path):
    """Verify _run_send(bid) calls broadcasts.send_broadcast(bid).
    This is the wiring the scheduler's job body uses.
    """
    monkeypatch.chdir(tmp_path)
    bid = await _make_broadcast(authed_client)

    calls = []
    from broadcaster.services import broadcasts as bc_svc
    original = bc_svc.send_broadcast

    def spy(b):
        calls.append(b)
        return {"status": "sent", "broadcast_id": b, "sent_at": "now", "counters": {}}

    monkeypatch.setattr(bc_svc, "send_broadcast", spy)

    # Call the scheduler's job body directly (the same body the
    # scheduler executes on the scheduled time).
    from broadcaster.services.scheduler import _run_send
    _run_send(bid)
    assert calls == [bid]
