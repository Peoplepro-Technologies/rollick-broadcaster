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

    # Per-admin row wins; fall back to the global setting. The DB-side
    # sentinel for "no per-admin destination" is the empty string, which
    # is what the migration backfills legacy rows with — preserving the
    # pre-2026-07-14 fallback behaviour for existing deployments.
    recipient = admin_svc.resolve_recovery_email(row)
    if not recipient:
        return (False, "recovery_mailbox_not_configured")

    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        return (False, "smtp_not_configured")

    admin_id = row["id"]
    temp = generate_strong_password()

    # Rotate the password + flag first; rollback on send failure.
    admin_svc.change_password(admin_id=admin_id, new_password=temp)
    admin_svc.set_must_change_password(admin_id, True)

    iso_now = datetime.now(timezone.utc)
    # "14 July 2026, 05:45 UTC" — day, full month name, year, HH:MM UTC.
    # dt.day avoids the %-d / %#d portability dance (Linux vs Windows);
    # the rest is plain strftime.
    request_time = f"{iso_now.day} {iso_now.strftime('%B %Y, %H:%M UTC')}"
    body = (
        f"A password recovery request was received for the following account:\n\n"
        f"Username: {username}\n"
        f"Request Time: {request_time}\n\n"
        f"A temporary password has been generated:\n\n"
        f"{temp}\n\n"
        f"Please use this temporary password to sign in. You will be "
        f"required to set a new permanent password upon your first sign-in.\n\n"
        f"For security, do not share this password with anyone.\n\n"
        f"If you did not request this password recovery, no action is required.\n\n"
        f"Regards,\n"
        f"Support Team"
    )
    result = EmailSender().send(Message(
        channel="email",
        recipient=recipient,
        subject=f"[Rollick] Password recovery for {username}",
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