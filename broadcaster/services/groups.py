"""Groups CRUD + auto-group rebuild.

Auto groups (one per distinct department, one per distinct location,
one per (dept × location) pair) are computed from `users` and rebuilt
on demand — typically after an Excel import.

Manual groups are stored with `is_auto=0` and have explicit rows in
`group_memberships`.

`resolve_recipients(group_ids, user_ids)` returns the deduplicated
set of user IDs for a broadcast's targets. Used by Phase 2.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException

from broadcaster.db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── List ──────────────────────────────────────────────────────

def list_groups() -> list[dict]:
    """All groups with live member counts."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, type, criteria, is_auto, created_at FROM groups ORDER BY is_auto DESC, name"
        ).fetchall()
        out = []
        for r in rows:
            count = _count_members(conn, dict(r))
            out.append({**dict(r), "is_auto": bool(r["is_auto"]), "member_count": count})
        return out


def _count_members(conn, group: dict) -> int:
    if group["is_auto"]:
        where, params = _auto_criteria_to_where(group["type"], group["name"])
        if where is None:
            return 0
        row = conn.execute(f"SELECT COUNT(*) AS n FROM users WHERE is_active = 1 AND {where}", params).fetchone()
        return row["n"]
    # Manual
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM group_memberships gm "
        "JOIN users u ON u.id = gm.user_id "
        "WHERE gm.group_id = ? AND u.is_active = 1",
        (group["id"],),
    ).fetchone()
    return row["n"]


def get_group(gid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute(
            "SELECT id, name, type, criteria, is_auto, created_at FROM groups WHERE id = ?",
            (gid,),
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["is_auto"] = bool(d["is_auto"])
    return d


def get_members(gid: int) -> list[dict]:
    """Return the active members of a group, regardless of auto/manual."""
    with get_db() as conn:
        g = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        if not g:
            return []
        if g["is_auto"]:
            where, params = _auto_criteria_to_where(g["type"], g["name"])
            if where is None:
                return []
            return [dict(r) for r in conn.execute(
                f"SELECT id, name, phone, email, department, location, is_active FROM users "
                f"WHERE is_active = 1 AND {where} ORDER BY name",
                params,
            ).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT u.id, u.name, u.phone, u.email, u.department, u.location, u.is_active "
            "FROM users u JOIN group_memberships gm ON gm.user_id = u.id "
            "WHERE gm.group_id = ? AND u.is_active = 1 ORDER BY u.name",
            (gid,),
        ).fetchall()]


# ── Create / update / delete ──────────────────────────────────

def create_manual_group(name: str, type_: str, criteria: Optional[str]) -> dict:
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name_required")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO groups (name, type, criteria, is_auto, created_at) VALUES (?, ?, ?, 0, ?)",
            (name.strip(), type_ or "manual", criteria, _now()),
        )
    return get_group(cur.lastrowid)  # type: ignore[return-value]


def update_group(gid: int, name: Optional[str] = None, criteria: Optional[str] = None,
                 type_: Optional[str] = None) -> Optional[dict]:
    sets, params = [], []
    if name is not None:
        sets.append("name = ?"); params.append(name.strip())
    if criteria is not None:
        sets.append("criteria = ?"); params.append(criteria)
    if type_ is not None:
        sets.append("type = ?"); params.append(type_)
    if not sets:
        return get_group(gid)
    params.append(gid)
    with get_db() as conn:
        conn.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id = ? AND is_auto = 0", params)
    return get_group(gid)


def delete_group(gid: int) -> bool:
    """Delete a manual group. Auto groups cannot be deleted (rebuild instead)."""
    with get_db() as conn:
        g = conn.execute("SELECT is_auto FROM groups WHERE id = ?", (gid,)).fetchone()
        if not g:
            return False
        if g["is_auto"]:
            raise HTTPException(status_code=400, detail="cannot_delete_auto_group")
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
    return True


# ── Membership (manual groups) ────────────────────────────────

def add_members(gid: int, user_ids: list[int]) -> int:
    with get_db() as conn:
        g = conn.execute("SELECT is_auto FROM groups WHERE id = ?", (gid,)).fetchone()
        if not g:
            raise HTTPException(status_code=404, detail="group_not_found")
        if g["is_auto"]:
            raise HTTPException(status_code=400, detail="auto_group_membership_derived")
        added = 0
        for uid in user_ids:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO group_memberships (group_id, user_id) VALUES (?, ?)",
                    (gid, uid),
                )
                added += conn.total_changes  # cumulative; we'll count differently
            except Exception:
                pass
        # Recount actual additions: count rows for this group that have one of the user_ids
        placeholders = ",".join("?" * len(user_ids))
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM group_memberships WHERE group_id = ? AND user_id IN ({placeholders})",
            [gid] + list(user_ids),
        ).fetchone()
        return row["n"]


def remove_member(gid: int, uid: int) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM group_memberships WHERE group_id = ? AND user_id = ?",
            (gid, uid),
        )
    return cur.rowcount > 0


# ── Auto-group rebuild ────────────────────────────────────────

def rebuild_auto_groups() -> dict:
    """Drop all auto groups, then create one per dept, one per location,
    one per (dept × location) pair, based on current active users.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM groups WHERE is_auto = 1")
        depts = [r["department"] for r in conn.execute(
            "SELECT DISTINCT department FROM users "
            "WHERE is_active = 1 AND department IS NOT NULL AND department != ''"
        ).fetchall()]
        locs = [r["location"] for r in conn.execute(
            "SELECT DISTINCT location FROM users "
            "WHERE is_active = 1 AND location IS NOT NULL AND location != ''"
        ).fetchall()]
        pairs = [r for r in conn.execute(
            "SELECT DISTINCT department, location FROM users "
            "WHERE is_active = 1 AND department IS NOT NULL AND department != '' "
            "AND location IS NOT NULL AND location != ''"
        ).fetchall()]

        now = _now()
        n = 0
        for d in depts:
            conn.execute(
                "INSERT INTO groups (name, type, criteria, is_auto, created_at) VALUES (?, ?, NULL, 1, ?)",
                (d, "department", now),
            )
            n += 1
        for l in locs:
            conn.execute(
                "INSERT INTO groups (name, type, criteria, is_auto, created_at) VALUES (?, ?, NULL, 1, ?)",
                (l, "location", now),
            )
            n += 1
        for p in pairs:
            conn.execute(
                "INSERT INTO groups (name, type, criteria, is_auto, created_at) VALUES (?, ?, NULL, 1, ?)",
                (f"{p['department']} / {p['location']}", "combo", now),
            )
            n += 1
    return {"created": n, "departments": len(depts), "locations": len(locs), "combos": len(pairs)}


def _auto_criteria_to_where(type_: str, name: str) -> tuple[Optional[str], list]:
    """Map an auto group's (type, name) to a SQL WHERE fragment against `users`.

    For department/location auto groups, `name` is the raw department/location
    value (no prefix). For combo groups, name is "<dept> / <loc>".
    """
    if type_ == "department":
        if not name:
            return None, []
        return "department = ?", [name]
    if type_ == "location":
        if not name:
            return None, []
        return "location = ?", [name]
    if type_ == "combo":
        if " / " not in name:
            return None, []
        dept, loc = name.split(" / ", 1)
        return "department = ? AND location = ?", [dept, loc]
    return None, []


# ── Recipient resolution (used by Phase 2) ───────────────────

def resolve_recipients(group_ids: Iterable[int], user_ids: Iterable[int]) -> list[int]:
    """Return the union of user IDs in the given groups plus the given user IDs.
    De-duped, active users only.
    """
    gids = list(set(group_ids))
    uids = list(set(user_ids))
    with get_db() as conn:
        result: set[int] = set()
        for uid in uids:
            row = conn.execute("SELECT 1 FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
            if row:
                result.add(uid)
        for gid in gids:
            members = get_members(gid)
            for m in members:
                result.add(m["id"])
    return sorted(result)
