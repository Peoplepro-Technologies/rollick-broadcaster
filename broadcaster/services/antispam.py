"""Anti-spam validation for the public comment endpoint.

Defense in depth (per BUILD_PLAN.md §7):
  1. Token-only access (handled by route — no anonymous enumeration)
  2. Token expiry / revoked (handled by links.resolve_token)
  3. Honeypot field (caller checks `website` is empty)
  4. Time-to-fill (caller passes ts_issued; we reject <2s or >2h)
  5. Per-IP rate (5/broadcast/hour, 20/global/hour)
  6. Per-token cap (3/lifetime)
  7. Per-session cooldown (30s)
  8. Profanity + link filter (≤1 http URL per comment)
  9. Body length (2..500)
 10. CSRF (Phase 8)

Phase 5 implements 3-9. Phase 8 adds CSP + CSRF token.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from broadcaster.db import get_db
from broadcaster.services import privacy
from broadcaster.settings import get_settings


HTTP_RE = re.compile(r"https?://", re.IGNORECASE)

# A tiny blocklist. Real deployment would load a curated list + use a
# normalization pass (leet speak) for fuzzy matches. v1 keeps it simple.
PROFANITY = {
    "badword1", "badword2", "damn", "shit", "fuck", "bitch", "asshole",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_body(body: Optional[str]) -> tuple[bool, str]:
    """Returns (ok, normalized_body_or_reason)."""
    if body is None or not isinstance(body, str):
        return False, "body_required"
    s = body.strip()
    if len(s) < 2:
        return False, "body_too_short"
    if len(s) > 500:
        return False, "body_too_long"
    if len(HTTP_RE.findall(s)) > 1:
        return False, "too_many_links"
    # Profanity (case-insensitive, whole-word)
    lowered = s.lower()
    for word in PROFANITY:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return False, "profanity"
    return True, s


def check_time_to_fill(ts_issued_ms: Optional[int]) -> tuple[bool, str]:
    """Caller minted a timestamp at page render; reject if the user submitted
    too quickly (bot) or after a long idle (stale tab)."""
    if ts_issued_ms is None:
        return False, "ts_issued_required"
    delta = (_now() - datetime.fromtimestamp(ts_issued_ms / 1000, tz=timezone.utc)).total_seconds()
    if delta < 2:
        return False, "submitted_too_fast"
    if delta > 7200:  # 2 hours
        return False, "stale_form"
    return True, "ok"


def check_honeypot(value: Optional[str]) -> bool:
    """Returns True if honeypot passed (field is empty)."""
    return not value


def check_per_token_cap(link_id: int) -> tuple[bool, str]:
    """A single link can post at most N comments in its lifetime."""
    s = get_settings()
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM comments WHERE link_id = ?",
            (link_id,),
        ).fetchone()["n"]
    if n >= s.comment_max_per_link_lifetime:
        return False, "per_token_cap_exceeded"
    return True, "ok"


def check_per_ip_rate(ip_hash: str, broadcast_id: int) -> tuple[bool, str]:
    """5 comments/broadcast/hour per IP, 20 global/hour."""
    s = get_settings()
    cutoff = (_now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with get_db() as conn:
        n_bcast = conn.execute(
            "SELECT COUNT(*) AS n FROM comments "
            "WHERE ip_hash = ? AND broadcast_id = ? AND created_at >= ?",
            (ip_hash, broadcast_id, cutoff),
        ).fetchone()["n"]
        n_global = conn.execute(
            "SELECT COUNT(*) AS n FROM comments "
            "WHERE ip_hash = ? AND created_at >= ?",
            (ip_hash, cutoff),
        ).fetchone()["n"]
    if n_bcast >= s.comment_max_per_ip_per_hour:
        return False, "per_ip_broadcast_rate_exceeded"
    if n_global >= 20:
        return False, "per_ip_global_rate_exceeded"
    return True, "ok"


def check_cooldown(link_id: int) -> tuple[bool, str]:
    """At most one comment per 30s per link. Cooldown=0 disables the check."""
    s = get_settings()
    if s.comment_cooldown_seconds <= 0:
        return True, "ok"
    cutoff = (_now() - timedelta(seconds=s.comment_cooldown_seconds)).isoformat(timespec="seconds")
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last_at FROM comments WHERE link_id = ? AND created_at >= ?",
            (link_id, cutoff),
        ).fetchone()
    if row and row["last_at"]:
        return False, "cooldown"
    return True, "ok"


def hash_for_ip(ip: str) -> str:
    return privacy.hash_ip(ip)
