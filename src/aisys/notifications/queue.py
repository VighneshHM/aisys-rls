"""The notification queue: enqueue now, deliver later, retry with a growing wait, give up after a few tries.

Enqueueing never talks to a provider, so a slow or broken email server can never delay or fail the event that caused
the message (a gate alarm, for example). `dispatch` is the separate delivery step, run from a button or the CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from aisys.models import Notification, utcnow
from aisys.notifications import reminders
from aisys.notifications.providers import (
    NotificationConfig,
    NotificationProvider,
    Outgoing,
    ProviderError,
)
from aisys.timeutil import aware, iso

log = logging.getLogger("aisys.notifications")


def enqueue(
    db: Session,
    *,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    user_id: int | None = None,
    event_id: int | None = None,
) -> Notification:
    row = Notification(
        channel=channel, recipient=recipient, subject=subject[:200], body=body, user_id=user_id, event_id=event_id
    )
    db.add(row)
    return row


def dispatch(
    db: Session,
    providers: dict[str, NotificationProvider],
    config: NotificationConfig,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict:
    """Try every queued message that is due. One message failing never stops the others."""
    now = now or utcnow()
    due = db.scalars(
        select(Notification)
        .where(Notification.status == "queued", Notification.next_attempt_at <= now)
        .order_by(Notification.id)
        .limit(limit)
    ).all()
    sent = retry_later = failed = cancelled = tried = 0
    for n in due:
        stale = reminders.still_relevant(db, n)
        if stale:  # a reminder about a loan that was returned or renewed is dropped, not sent
            n.status, n.last_error = "cancelled", stale
            cancelled += 1
            db.commit()
            continue
        # Claim it: only one dispatcher (this app's loop, the command line, a button) wins the row, so a message is
        # never sent twice however many run at once. A crash after the claim means one more try later, not a loss.
        claimed = db.execute(
            update(Notification)
            .where(Notification.id == n.id, Notification.status == "queued", Notification.attempts == n.attempts)
            .values(attempts=n.attempts + 1)
        ).rowcount
        db.commit()
        if claimed != 1:
            continue
        db.refresh(n)
        tried += 1
        provider = providers.get(n.channel)
        try:
            if provider is None:
                raise ProviderError(f"no {n.channel} provider is configured")
            n.provider_ref = provider.send(Outgoing(n.channel, n.recipient, n.subject, n.body, n.id))
            n.status, n.sent_at, n.last_error = "sent", now, ""
            sent += 1
        except ProviderError as exc:
            n.last_error = str(exc)[:300]
            if n.attempts >= config.max_attempts:
                n.status = "failed"
                failed += 1
                log.error("notification %s given up after %d attempts: %s", n.id, n.attempts, exc)
            else:
                n.next_attempt_at = now + timedelta(seconds=config.backoff_s * n.attempts)
                retry_later += 1
        db.commit()  # each message is settled on its own
    next_at = db.scalar(select(func.min(Notification.next_attempt_at)).where(Notification.status == "queued"))
    return {
        "tried": tried + cancelled,
        "sent": sent,
        "retry_later": retry_later,
        "failed": failed,
        "cancelled": cancelled,
        "waiting": counts(db)["queued"],
        "next_attempt_at": iso(next_at),  # when the earliest waiting message is due again
    }


def counts(db: Session) -> dict[str, int]:
    rows = dict(db.execute(select(Notification.status, func.count()).group_by(Notification.status)).all())
    return {
        "queued": rows.get("queued", 0),
        "sent": rows.get("sent", 0),
        "failed": rows.get("failed", 0),
        "cancelled": rows.get("cancelled", 0),
    }


def retry(db: Session, notification_id: int) -> Notification | None:
    """Put a failed message back in the queue for another round of attempts."""
    n = db.get(Notification, notification_id)
    if n is None or n.status != "failed":
        return None
    n.status, n.attempts, n.next_attempt_at = "queued", 0, utcnow()
    db.commit()
    return n


def notification_out(n: Notification) -> dict:
    return {
        "id": n.id,
        "created_at": iso(n.created_at),
        "channel": n.channel,
        "recipient": n.recipient,
        "subject": n.subject,
        "body": n.body,
        "status": n.status,
        "attempts": n.attempts,
        "last_error": n.last_error,
        "next_attempt_at": iso(n.next_attempt_at),
        "sent_at": iso(n.sent_at),
        "provider_ref": n.provider_ref,
        "event_id": n.event_id,
        "kind": n.kind,
        "loan_id": n.loan_id,
        "member_id": n.member_id,
        "due": n.status == "queued" and aware(n.next_attempt_at) <= utcnow(),
    }


def list_notifications(
    db: Session, *, status: str = "", event_id: int | None = None, limit: int = 30, offset: int = 0
) -> dict:
    q = select(Notification)
    if status:
        q = q.where(Notification.status == status)
    if event_id is not None:
        q = q.where(Notification.event_id == event_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    # messages that still need attention (queued, failed) come first, so a backlog of sent ones cannot hide them
    unfinished_first = case((Notification.status == "sent", 1), else_=0)
    rows = db.scalars(q.order_by(unfinished_first, Notification.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "counts": counts(db), "notifications": [notification_out(n) for n in rows]}
