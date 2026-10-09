"""Patron reminders: tell a member their loan is due soon (a set number of days before) and on the due day.

`scan` looks at every open loan and queues one message per channel the member has an address for. A reminder
exists at most once for a (loan, kind, channel, due date): the database enforces that, so scanning every minute,
restarting the app or running two schedulers can never send one twice, and a renewal (a new due date) gets fresh
reminders. Delivery is the queue's job (`queue.dispatch`), with its retries; a reminder for a loan that was
returned or renewed in the meantime is cancelled instead of sent.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aisys.models import Loan, Member, Notification, utcnow
from aisys.notifications import messages
from aisys.notifications.providers import NotificationConfig, valid_address
from aisys.timeutil import aware

log = logging.getLogger("aisys.notifications")


def library_day(moment: datetime) -> date:
    """Whole days in UTC, the same rule the reports use (assumption A-20)."""
    return aware(moment).astimezone(UTC).date()


def pretty(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def _addresses(member: Member) -> list[tuple[str, str]]:
    found = []
    if valid_address("email", member.email):
        found.append(("email", member.email))
    if valid_address("sms", member.phone):
        found.append(("sms", member.phone))
    return found


def scan(
    db: Session, config: NotificationConfig, *, library_name: str = "The library", now: datetime | None = None
) -> dict:
    """Queue the reminders that are due today. Safe to run as often as you like."""
    result = {"queued": 0, "due_soon": 0, "due_today": 0, "already_sent": 0, "no_contact": 0, "opted_out": 0}
    if not config.reminders:
        return result
    now = aware(now or utcnow())
    today = library_day(now)
    soon = today + timedelta(days=config.remind_days_before)
    start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    end = datetime(soon.year, soon.month, soon.day, tzinfo=UTC) + timedelta(days=1)
    loans = db.scalars(
        select(Loan).where(Loan.returned_at.is_(None), Loan.due_at >= start, Loan.due_at < end).order_by(Loan.id)
    ).all()
    for loan in loans:
        day = library_day(loan.due_at)
        kind = "due_today" if day == today else "due_soon" if day == soon else ""
        if not kind:
            continue
        member = loan.member
        if not member.reminders_on:
            result["opted_out"] += 1
            continue
        addresses = _addresses(member)
        if not addresses:
            result["no_contact"] += 1
            continue
        have = set(
            db.scalars(
                select(Notification.channel).where(
                    Notification.loan_id == loan.id, Notification.kind == kind, Notification.due_on == str(day)
                )
            )
        )
        fresh = [(c, a) for c, a in addresses if c not in have]
        if not fresh:
            result["already_sent"] += 1
            continue
        for channel, address in fresh:
            subject, body = messages.patron_reminder(
                channel,
                kind,
                member_name=member.name,
                title=loan.item.bib.title,
                accession_no=loan.item.accession_no,
                due_day=pretty(day),
                library=library_name,
                days_before=config.remind_days_before,
            )
            db.add(
                Notification(
                    channel=channel,
                    recipient=address,
                    subject=subject,
                    body=body,
                    kind=kind,
                    member_id=member.id,
                    loan_id=loan.id,
                    due_on=str(day),
                    next_attempt_at=now,  # due from the scan's own clock, not the wall clock (D-051)
                )
            )
        try:
            db.commit()
        except IntegrityError:  # another scheduler queued it a moment ago: the unique index did its job
            db.rollback()
            result["already_sent"] += 1
            continue
        result["queued"] += len(fresh)
        result[kind] += len(fresh)
    if result["queued"]:
        log.info("queued %d patron reminder(s): %s", result["queued"], result)
    return result


def still_relevant(db: Session, n: Notification) -> str:
    """'' if a queued reminder should still go out, otherwise why it should not."""
    if not n.loan_id:
        return ""
    loan = db.get(Loan, n.loan_id)
    if loan is None or loan.returned_at is not None:
        return "The item was returned before the reminder was sent."
    if str(library_day(loan.due_at)) != n.due_on:
        return "The loan was renewed, so this reminder no longer applies."
    return ""
