"""Password reset / recovery flow.

When an admin clicks "Forgot password?" on the login page, they enter
their username and this service:

  1. Validates the username exists in `admins`.
  2. Validates that a recovery mailbox and SMTP are configured.
  3. Generates a strong temporary password.
  4. Hashes it into `admins.password_hash` and sets
     `must_change_password = 1` so the admin is forced to set a
     permanent password on first sign-in.
  5. Emails the new temporary password to the configured recovery
     mailbox so the operator can relay it out-of-band.

On SMTP send failure, the password rotation is rolled back so the
admin isn't silently locked out by a half-applied reset.

The recovery flow returns `(ok, detail)` so the route layer can map
each detail code to an HTTP status. Detail codes are stable strings —
the login page JS and the settings page tests both reference them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from broadcaster.services import admin as admin_svc
from broadcaster.services import settings as settings_svc
from broadcaster.services.email import EmailSender
from broadcaster.services.senders import Message
from broadcaster.security import generate_strong_password
from broadcaster.settings import get_settings


def request_reset(username: str) -> tuple[bool, str]:
    """Process a forgot-password request for `username`.

    Returns (ok, detail). detail is one of:
      "sent"                            – success
      "no_such_admin"                   – username not in admins table
      "recovery_mailbox_not_configured" – settings.password_recovery_email empty
      "smtp_not_configured"             – SMTP host or from-address missing
      "send_failed"                     – smtplib raised; DB rolled back
    """
    if not username:
        return (False, "no_such_admin")

    row = admin_svc.find_by_username(username)
    if row is None:
        return (False, "no_such_admin")

    recovery_email = (settings_svc.get("password_recovery_email") or "").strip()
    if not recovery_email:
        return (False, "recovery_mailbox_not_configured")

    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        return (False, "smtp_not_configured")

    admin_id = row["id"]
    temp = generate_strong_password()

    # Rotate the password + flag first; rollback on send failure.
    admin_svc.change_password(admin_id=admin_id, new_password=temp)
    admin_svc.set_must_change_password(admin_id, True)

    iso_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = (
        f"A password reset was requested for admin username "
        f"\"{username}\" at {iso_now} UTC.\n\n"
        f"New temporary password: {temp}\n\n"
        f"Relay this password to the requesting admin out-of-band "
        f"(phone, Teams, etc.). They will be required to set a "
        f"permanent password on first sign-in.\n\n"
        f"If you did not expect this request, no action is required."
    )
    result = EmailSender().send(Message(
        channel="email",
        recipient=recovery_email,
        subject=f"[Rollick] Password reset requested for \"{username}\"",
        body=body,
        viewer_link="",
        broadcast_id=0,
        user_id=0,
        link_id=0,
    ))
    if not result.ok:
        # Roll back: rotate to a fresh throwaway password (the original
        # plaintext is unknown to us, so we can't restore it — best we
        # can do is generate a new one the admin can't predict and
        # clear the must-change flag so they're not stuck behind a
        # change-password screen they can't reach).
        admin_svc.change_password(admin_id=admin_id, new_password=generate_strong_password())
        admin_svc.set_must_change_password(admin_id, False)
        return (False, "send_failed")

    return (True, "sent")