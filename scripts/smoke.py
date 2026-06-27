#!/usr/bin/env python3
"""End-to-end smoke test for Rollick Broadcaster.

Boots the FastAPI app in-process (via httpx + ASGITransport), runs the
full pipeline against a fresh temp DB, and prints PASS/FAIL per step.

Covers:
  - admin login
  - user creation
  - text content creation
  - broadcast create (auto-mints per-user links)
  - send fan-out (MockSender writes to sent_log/)
  - public viewer GET (records a view)
  - anonymous comment POST (passes time-to-fill + honeypot + body check)
  - analytics rollup (link_count, total_views, comment_count)
  - link revocation (viewer returns 410 for revoked tokens)

Usage:
  python scripts/smoke.py
  python scripts/smoke.py --keep-on-fail   # keep tmpdir on failure for debugging

Exit code: 0 on full pass, 1 on any failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable

# ── Env setup BEFORE app imports (settings is lru_cache) ──────

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="broadcaster-smoke-"))
os.environ["DATABASE_URL"] = str(TMP / "smoke.db")
os.environ["SESSION_SECRET"] = "smoke-secret-32-chars-padding-aaaa"
os.environ["IP_HASH_PEPPER"] = "smoke-pepper"
os.environ["ADMIN_PASSWORD"] = "smoke-admin-pass"
os.environ["COMMENT_COOLDOWN_SECONDS"] = "0"
os.environ["COMMENT_MAX_PER_LINK_LIFETIME"] = "3"
os.environ["LINK_TOKEN_TTL_DAYS"] = "30"
os.environ["BASE_PUBLIC_URL"] = "http://smoke.test"
# Run from the tmp dir so MockSender writes sent_log/ there (not in the repo).
os.chdir(TMP)

sys.path.insert(0, str(ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from broadcaster import settings as settings_mod  # noqa: E402
settings_mod.get_settings.cache_clear()

from app import app as fastapi_app  # noqa: E402
from broadcaster.db import init_db  # noqa: E402
from broadcaster.services.admin import bootstrap_admin  # noqa: E402
from broadcaster.services import scheduler as sched_svc  # noqa: E402
from broadcaster.services.senders import SENT_LOG_DIR  # noqa: E402


# ── Output helpers ─────────────────────────────────────────────

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def passed(name: str) -> None:
    print(f"  {GREEN}[PASS]{RESET} {name}")


def failed(name: str, reason: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {name}: {reason}")


StepResult = tuple[str, Callable[[AsyncClient], Awaitable[None]]]


def _step(name: str, fn: Callable[[AsyncClient], Awaitable[None]]) -> StepResult:
    return (name, fn)


# ── Steps ─────────────────────────────────────────────────────

async def s_health(c: AsyncClient) -> None:
    r = await c.get("/api/health")
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


async def s_login(c: AsyncClient) -> None:
    r = await c.post(
        "/api/auth/login",
        data={"username": "admin", "password": "smoke-admin-pass"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, f"login status={r.status_code} body={r.text}"
    assert r.json()["ok"] is True
    r1 = await c.get("/api/auth/me")
    assert r1.status_code == 200, f"me status={r1.status_code}"
    assert r1.json()["username"] == "admin"


async def s_create_users(c: AsyncClient) -> list[int]:
    ids: list[int] = []
    for i in range(3):
        body = {
            "name": f"Smoke User {i}",
            "phone": f"5{100000000 + i:09d}",  # 10 digits, unique
            "email": f"smoke{i}@example.com",
            "department": "Sales" if i % 2 == 0 else "Ops",
            "location": "Mumbai" if i < 2 else "Delhi",
        }
        r = await c.post("/api/users", json=body)
        assert r.status_code == 200, f"create user {i}: {r.status_code} {r.text}"
        ids.append(r.json()["id"])
    assert len(ids) == 3
    return ids


async def s_create_content(c: AsyncClient) -> int:
    r = await c.post("/api/content/text", json={
        "caption": "Smoke caption",
        "body": "Hello from the smoke test.",
    })
    assert r.status_code == 200, f"create text: {r.status_code} {r.text}"
    return r.json()["id"]


async def s_create_broadcast(c: AsyncClient, user_ids: list[int], content_id: int) -> int:
    r = await c.post("/api/broadcasts", json={
        "title": "Smoke broadcast",
        "category": "Test",
        "message_text": "Open this: {{viewer_link}}",
        "content_id": content_id,
        "delivery_channel": "whatsapp",
        "user_ids": user_ids,
        "generate_links": True,
    })
    assert r.status_code == 200, f"create broadcast: {r.status_code} {r.text}"
    body = r.json()
    assert body["status"] == "draft"
    assert body["link_info"]["created"] == 3
    assert body["link_info"]["skipped_existing"] == 0
    return body["id"]


async def s_send_broadcast(c: AsyncClient, bid: int, tmpdir: Path) -> None:
    r = await c.post(f"/api/broadcasts/{bid}/send")
    assert r.status_code == 200, f"send: {r.status_code} {r.text}"
    body = r.json()
    assert body["status"] == "sent", f"expected sent, got {body}"
    assert body["counters"]["whatsapp"]["sent"] == 3
    assert body["counters"]["whatsapp"]["failed"] == 0
    # MockSender writes JSON files to sent_log/{channel}/
    log_dir = tmpdir / SENT_LOG_DIR / "whatsapp"
    files = list(log_dir.glob("*.json"))
    assert len(files) == 3, f"expected 3 mock-send files in {log_dir}, got {len(files)}"
    blob = json.loads(files[0].read_text())
    assert "viewer_link" in blob
    assert blob["channel"] == "whatsapp"
    assert blob["broadcast_id"] == bid


async def s_get_token(c: AsyncClient, bid: int) -> str:
    r = await c.get(f"/api/broadcasts/{bid}/links")
    assert r.status_code == 200, f"list_links: {r.status_code} {r.text}"
    links = r.json()
    assert len(links) == 3, f"expected 3 links, got {len(links)}"
    return links[0]["token"]


async def s_visitor_views_page(c: AsyncClient, token: str) -> None:
    r = await c.get(f"/v/{token}", headers={"User-Agent": "smoke-test/1.0"})
    assert r.status_code == 200, f"viewer: {r.status_code} body={r.text[:200]}"
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    assert "Smoke broadcast" in html, "viewer page missing broadcast title"
    assert "anonymous" in html.lower(), "viewer page missing anonymity notice"


async def s_post_comment(c: AsyncClient, token: str) -> None:
    # ts_issued must be >= 2s ago for time-to-fill check
    ts_issued = int((time.time() - 3) * 1000)
    r = await c.post(
        f"/v/{token}/comments",
        data={
            "body": "Great broadcast, thanks!",
            "website": "",          # honeypot — must be empty
            "ts_issued": str(ts_issued),
        },
    )
    assert r.status_code == 200, f"comment: {r.status_code} body={r.text}"
    body = r.json()
    assert "id" in body and body["id"] > 0


async def s_analytics(c: AsyncClient, bid: int) -> None:
    r = await c.get(f"/api/broadcasts/{bid}/analytics")
    assert r.status_code == 200, f"analytics: {r.status_code} {r.text}"
    body = r.json()
    assert body["broadcast_id"] == bid
    totals = body["totals"]
    assert totals["link_count"] == 3, f"link_count={totals['link_count']}"
    assert totals["viewed_count"] == 1, f"viewed_count={totals['viewed_count']}"
    assert totals["total_views"] >= 1, f"total_views={totals['total_views']}"
    assert totals["comment_count"] == 1, f"comment_count={totals['comment_count']}"
    assert totals["unique_ips"] >= 1


async def s_revoke_link(c: AsyncClient, bid: int) -> None:
    r = await c.get(f"/api/broadcasts/{bid}/links")
    assert r.status_code == 200
    links = r.json()
    # Pick a link not used by the earlier viewer step
    target = next(l for l in links if l["first_viewed_at"] is None)
    r1 = await c.post(f"/api/broadcasts/{bid}/links/{target['id']}/revoke")
    assert r1.status_code == 200, f"revoke: {r1.status_code} {r1.text}"
    r2 = await c.get(f"/v/{target['token']}")
    assert r2.status_code == 410, f"expected 410 for revoked, got {r2.status_code}"


# ── Main ──────────────────────────────────────────────────────

async def main(keep_on_fail: bool) -> int:
    failures: list[tuple[str, str]] = []
    passed_count = 0

    print(f"\n{DIM}tmpdir: {TMP}{RESET}")
    print(f"{DIM}db:     {os.environ['DATABASE_URL']}{RESET}\n")

    # Bootstrap DB + scheduler manually (lifespan doesn't run under ASGITransport).
    init_db()
    bootstrap_admin()
    sched_svc.start()

    transport = ASGITransport(app=fastapi_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Each step is (name, async fn(client)). Stateful data
            # (broadcast id, token, user_ids) flows via local variables
            # so failures in one step don't poison later steps.
            user_ids: list[int] = []
            content_id = 0
            bid = 0
            token = ""

            try:
                await s_health(client); passed("GET /api/health")
            except (AssertionError, Exception) as e:
                failures.append(("GET /api/health", f"{type(e).__name__}: {e}"))

            try:
                await s_login(client); passed("POST /api/auth/login (form)")
            except (AssertionError, Exception) as e:
                failures.append(("POST /api/auth/login (form)", f"{type(e).__name__}: {e}"))

            try:
                user_ids = await s_create_users(client)
                passed(f"POST /api/users x3 (ids={user_ids})")
            except (AssertionError, Exception) as e:
                failures.append(("POST /api/users x3", f"{type(e).__name__}: {e}"))

            try:
                content_id = await s_create_content(client)
                passed(f"POST /api/content/text (id={content_id})")
            except (AssertionError, Exception) as e:
                failures.append(("POST /api/content/text", f"{type(e).__name__}: {e}"))

            if user_ids and content_id:
                try:
                    bid = await s_create_broadcast(client, user_ids, content_id)
                    passed(f"POST /api/broadcasts (id={bid}, 3 links minted)")
                except (AssertionError, Exception) as e:
                    failures.append(("POST /api/broadcasts", f"{type(e).__name__}: {e}"))
            else:
                failures.append(("POST /api/broadcasts", "skipped (prereq failed)"))

            if bid:
                try:
                    await s_send_broadcast(client, bid, TMP)
                    passed("POST /api/broadcasts/{id}/send (3 mock files on disk)")
                except (AssertionError, Exception) as e:
                    failures.append(("POST /api/broadcasts/{id}/send", f"{type(e).__name__}: {e}"))

                try:
                    token = await s_get_token(client, bid)
                    passed(f"GET /api/broadcasts/{bid}/links (got token)")
                except (AssertionError, Exception) as e:
                    failures.append(("GET /api/broadcasts/{bid}/links", f"{type(e).__name__}: {e}"))
            else:
                failures.append(("POST /api/broadcasts/{id}/send", "skipped (no bid)"))
                failures.append(("GET /api/broadcasts/{bid}/links", "skipped (no bid)"))

            if token:
                try:
                    await s_visitor_views_page(client, token)
                    passed("GET /v/{token} (public viewer, view recorded)")
                except (AssertionError, Exception) as e:
                    failures.append(("GET /v/{token}", f"{type(e).__name__}: {e}"))

                try:
                    await s_post_comment(client, token)
                    passed("POST /v/{token}/comments (anonymous)")
                except (AssertionError, Exception) as e:
                    failures.append(("POST /v/{token}/comments", f"{type(e).__name__}: {e}"))
            else:
                failures.append(("GET /v/{token}", "skipped (no token)"))
                failures.append(("POST /v/{token}/comments", "skipped (no token)"))

            if bid:
                try:
                    await s_analytics(client, bid)
                    passed(f"GET /api/broadcasts/{bid}/analytics (counters match)")
                except (AssertionError, Exception) as e:
                    failures.append(("GET /api/broadcasts/{bid}/analytics", f"{type(e).__name__}: {e}"))

                try:
                    await s_revoke_link(client, bid)
                    passed("POST .../links/{id}/revoke + GET /v/{token} → 410")
                except (AssertionError, Exception) as e:
                    failures.append(("POST .../links/{id}/revoke", f"{type(e).__name__}: {e}"))
            else:
                failures.append(("GET analytics", "skipped (no bid)"))
                failures.append(("Revoke + 410 check", "skipped (no bid)"))
    finally:
        try:
            sched_svc.shutdown()
        except Exception:
            pass

    total = len(failures) + (
        sum(1 for _ in [
            "health", "login", "create_users", "create_content",
            "create_bc", "send", "get_token", "view", "comment",
            "analytics", "revoke",
        ])
    )
    passed_count = total - len(failures)

    print()
    if failures:
        print(f"{RED}{len(failures)}/{total} steps FAILED{RESET}")
        for name, reason in failures:
            print(f"  {RED}✗{RESET} {name}: {reason}")
        if keep_on_fail:
            print(f"\n{DIM}tmpdir preserved for debugging: {TMP}{RESET}")
        else:
            shutil.rmtree(TMP, ignore_errors=True)
        return 1

    print(f"{GREEN}{total}/{total} steps PASSED{RESET}")
    if keep_on_fail:
        print(f"{DIM}tmpdir preserved: {TMP}{RESET}")
    else:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--keep-on-fail",
        action="store_true",
        help="Preserve tmpdir on failure for debugging.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.keep_on_fail)))
