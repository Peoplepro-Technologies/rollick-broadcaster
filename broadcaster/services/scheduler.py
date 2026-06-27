"""APScheduler integration: fire scheduled broadcasts at the right time.

The scheduler is process-local. On startup, `rehydrate_pending` finds
all broadcasts with status='queued' and scheduled_at > now and adds
them to the scheduler. A periodic job (every 30s) re-runs the same
query so broadcasts queued *after* startup are also picked up.

When a queued broadcast's scheduled_at is in the past (e.g. the app
was down), the rehydrate job fires it immediately on its next tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from broadcaster.db import get_db


log = logging.getLogger("broadcaster.scheduler")

_scheduler: Optional[AsyncIOScheduler] = None
_started: bool = False


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def _job_id(bid: int) -> str:
    return f"broadcast:{bid}"


def start() -> None:
    """Start the scheduler and run an initial rehydrate."""
    global _started
    if _started:
        return
    s = get_scheduler()
    if not s.running:
        s.start()
    # Re-check every 30s for newly-queued broadcasts.
    s.add_job(rehydrate_pending, "interval", seconds=30,
              id="rehydrate_pending", replace_existing=True, max_instances=1)
    # Run once at startup so existing queued broadcasts are picked up.
    rehydrate_pending()
    _started = True


def shutdown() -> None:
    global _started
    s = get_scheduler()
    if s.running:
        s.shutdown(wait=False)
    _started = False


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def schedule_broadcast(bid: int, when_iso: str) -> None:
    """Add (or replace) a one-shot job that fires send_broadcast at when_iso."""
    s = get_scheduler()
    run_at = _parse_iso(when_iso)
    s.add_job(
        _run_send,
        trigger=DateTrigger(run_date=run_at, timezone="UTC"),
        args=[bid],
        id=_job_id(bid),
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,  # 10 min late is OK; later = skip
    )
    log.info("scheduled broadcast %d for %s", bid, run_at)


def cancel_broadcast(bid: int) -> None:
    s = get_scheduler()
    try:
        s.remove_job(_job_id(bid))
    except Exception:
        pass


def rehydrate_pending() -> int:
    """Find all queued broadcasts with scheduled_at and schedule them.
    Returns the number of jobs scheduled.
    """
    s = get_scheduler()
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, scheduled_at FROM broadcasts "
            "WHERE status = 'queued' AND scheduled_at IS NOT NULL"
        ).fetchall()
    n = 0
    for r in rows:
        bid = r["id"]
        run_at = _parse_iso(r["scheduled_at"])
        if run_at <= now:
            # Overdue — fire immediately on the scheduler.
            run_at = now + _ONE_SEC
        try:
            s.add_job(
                _run_send,
                trigger=DateTrigger(run_date=run_at, timezone="UTC"),
                args=[bid],
                id=_job_id(bid),
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=600,
            )
            n += 1
        except Exception as e:
            log.warning("failed to schedule broadcast %d: %s", bid, e)
    if n:
        log.info("rehydrate_pending: scheduled %d broadcast(s)", n)
    return n


def _run_send(bid: int) -> None:
    """The job body. Imported lazily to avoid circulars at module load."""
    from broadcaster.services.broadcasts import send_broadcast
    try:
        result = send_broadcast(bid)
        log.info("scheduled send broadcast %d: %s", bid, result.get("status"))
    except Exception as e:
        log.exception("scheduled send broadcast %d failed: %s", bid, e)


# Tiny delta used when rehydrating overdue broadcasts
import datetime as _dt
_ONE_SEC = _dt.timedelta(seconds=1)
