"""Role-based access control for staff users.

The viewer (broadcaster/routes/viewer.py) is unauthenticated; this
module governs the admin app only. Four lanes, four roles:

    super_admin    — full access including admin management
    hr_admin       — users + groups
    content_admin  — content + broadcasts + comments
    management     — read-only access (settings, dashboard, etc.)

Routes declare the roles that can hit them via the dependency
factory `Depends(require_role(*roles))`. This is the single
source of truth for capability mapping — there is no separate
config file.

Lockout safety: services/admin.py raises LastSuperAdminError when
an operation would drop the super_admin count below 1; this module
does not enforce that guard (it lives at the data-mutation layer).
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status


# Public type alias for the four roles. Kept as a plain string union
# rather than enum so it can be passed to/from sqlite TEXT columns
# without conversion.
Role = str  # "super_admin" | "hr_admin" | "content_admin" | "management"


ROLE_LANES: dict[str, set[str]] = {
    "super_admin":   {
        "users", "groups", "content", "broadcasts", "comments",
        "settings", "admins", "view:any",
    },
    "hr_admin":      {"users", "groups"},
    "content_admin": {"content", "broadcasts", "comments"},
    "management":    {"view:any"},
}


ROLE_RANK: dict[str, int] = {
    "super_admin":   4,
    "hr_admin":      3,
    "content_admin": 2,
    "management":    1,
}


SESSION_KEY = "admin_id"


@dataclass(frozen=True)
class AdminUser:
    """Lightweight handle on the currently-authenticated admin."""

    id: int
    username: str
    role: str


class ForbiddenForRole(Exception):
    """Raised internally by require_role; FastAPI translates to 403."""


def load_current_admin(request: Request) -> AdminUser:
    """Read the session cookie, fetch the admin row, attach to
    `request.state.current_admin` so templates can read it.

    Raises 401 if no session or admin row not found.
    """
    from broadcaster.services import admin as admin_svc

    admin_id = request.session.get(SESSION_KEY)
    if admin_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
        )
    row = admin_svc.find_by_id(admin_id)
    if row is None:
        # Session points to a deleted admin; clear and 401.
        request.session.pop(SESSION_KEY, None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_not_found",
        )
    user = AdminUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
    )
    request.state.current_admin = user
    return user


def require_role(*allowed: str):
    """Factory: returns a FastAPI dependency that allows only the listed roles.

    Usage:
        @router.get("/admin/users",
                    dependencies=[Depends(require_role(
                        "super_admin", "hr_admin", "management",
                    ))])

    Or with a return value:
        def list_users(_admin: AdminUser = Depends(require_role(...))): ...

    Raises 403 if the current admin's role is not in `allowed`.
    """
    allowed_set = frozenset(allowed)

    def _dep(request: Request) -> AdminUser:
        user = load_current_admin(request)
        if user.role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="forbidden_for_role",
            )
        return user

    return _dep
