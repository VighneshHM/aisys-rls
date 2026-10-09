"""Serials: subscriptions to journals and magazines, the issues expected, and the ones that arrived (or did not).

A subscription has a frequency; its issues are scheduled ahead from the first expected date. Receiving an issue records
the date and (optionally) books a copy into the catalogue so it can be tagged and lent. An issue that has not arrived a
week after its date is *late*; staff can claim it from the supplier or mark it missing.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.catalogue import service as catalogue
from aisys.models import SerialIssue, SerialTitle
from aisys.timeutil import iso

FREQUENCIES = ("weekly", "monthly", "quarterly", "yearly")
LATE_AFTER_DAYS = 7
MAX_AHEAD = 24
ISSN_RE = re.compile(r"\d{4}-\d{3}[\dXx]")


class SerialError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def issn_valid(issn: str) -> bool:
    """ISSN check digit: weights 8 to 2 over the first seven digits, modulo 11 (X means 10)."""
    if not ISSN_RE.fullmatch(issn):
        return False
    digits = issn.replace("-", "").upper()
    total = sum((8 - i) * int(c) for i, c in enumerate(digits[:7]))
    check = (11 - total % 11) % 11
    return digits[7] == ("X" if check == 10 else str(check))


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def next_date(day: date, frequency: str) -> date:
    if frequency == "weekly":
        return day + timedelta(days=7)
    return add_months(day, {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency])


def label_for(day: date, frequency: str) -> str:
    if frequency == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if frequency == "monthly":
        return f"{day.year}-{day.month:02d}"
    if frequency == "quarterly":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    return str(day.year)


def issue_out(i: SerialIssue, today: date | None = None) -> dict:
    today = today or date.today()
    late = i.status == "expected" and i.expected_on < today - timedelta(days=LATE_AFTER_DAYS)
    return {
        "id": i.id,
        "serial_id": i.serial_id,
        "label": i.label,
        "expected_on": i.expected_on.isoformat(),
        "received_on": i.received_on.isoformat() if i.received_on else None,
        "status": i.status,
        "late": late,
        "item_id": i.item_id,
        "note": i.note,
    }


def title_out(s: SerialTitle, *, with_issues: bool = False, today: date | None = None) -> dict:
    today = today or date.today()
    counts = {k: sum(1 for i in s.issues if i.status == k) for k in ("expected", "received", "claimed", "missing")}
    late = sum(
        1 for i in s.issues if i.status == "expected" and i.expected_on < today - timedelta(days=LATE_AFTER_DAYS)
    )
    out = {
        "id": s.id,
        "title": s.title,
        "issn": s.issn,
        "frequency": s.frequency,
        "supplier": s.supplier,
        "location": s.location,
        "active": s.active,
        "created_at": iso(s.created_at),
        "counts": {**counts, "late": late},
        "next_expected": next((i.expected_on.isoformat() for i in s.issues if i.status == "expected"), None),
    }
    if with_issues:
        out["issues"] = [issue_out(i, today) for i in s.issues]
    return out


def get_title(db: Session, serial_id: int) -> SerialTitle:
    s = db.get(SerialTitle, serial_id)
    if s is None:
        raise SerialError(404, "No such subscription")
    return s


def list_titles(db: Session) -> list[dict]:
    return [title_out(s) for s in db.scalars(select(SerialTitle).order_by(SerialTitle.title))]


def _schedule(s: SerialTitle, start: date, count: int) -> None:
    day = start
    have = {i.label for i in s.issues}
    for _ in range(count):
        label = label_for(day, s.frequency)
        if label not in have:
            s.issues.append(SerialIssue(label=label, expected_on=day))
            have.add(label)
        day = next_date(day, s.frequency)


def create_title(
    db: Session,
    *,
    title: str,
    issn: str,
    frequency: str,
    supplier: str,
    location: str,
    first_expected_on: date,
    issues_ahead: int = 6,
    actor: str,
    ip: str = "",
) -> SerialTitle:
    title = " ".join((title or "").split())
    if not title or len(title) > 500:
        raise SerialError(422, "A title of 1 to 500 characters is required")
    issn = (issn or "").strip().upper()
    if issn and not issn_valid(issn):
        raise SerialError(
            422, "That ISSN is not valid (four digits, a dash, three digits and a check digit, like 0378-5955)"
        )
    if frequency not in FREQUENCIES:
        raise SerialError(422, f"Frequency must be one of: {', '.join(FREQUENCIES)}")
    if not 1 <= issues_ahead <= MAX_AHEAD:
        raise SerialError(422, f"Schedule 1 to {MAX_AHEAD} issues ahead")
    if not date(2000, 1, 1) <= first_expected_on <= date(2100, 12, 31):
        raise SerialError(422, "The first expected date looks wrong")
    s = SerialTitle(
        title=title, issn=issn, frequency=frequency, supplier=" ".join((supplier or "").split())[:200],
        location=" ".join((location or "").split())[:100],
    )  # fmt: skip
    _schedule(s, first_expected_on, issues_ahead)
    db.add(s)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="serial.create",
        entity="serial",
        entity_id=s.id,
        detail={"title": title, "frequency": frequency},
        ip=ip,
    )
    db.commit()
    return s


def extend(db: Session, serial_id: int, count: int, *, actor: str, ip: str = "") -> SerialTitle:
    s = get_title(db, serial_id)
    if not 1 <= count <= MAX_AHEAD:
        raise SerialError(422, f"Schedule 1 to {MAX_AHEAD} more issues")
    last = max((i.expected_on for i in s.issues), default=None)
    if last is None:
        raise SerialError(409, "This subscription has no issues to continue from")
    _schedule(s, next_date(last, s.frequency), count)
    audit.record(
        db, actor=actor, action="serial.extend", entity="serial", entity_id=s.id, detail={"count": count}, ip=ip
    )
    db.commit()
    return s


def set_active(db: Session, serial_id: int, active: bool, *, actor: str, ip: str = "") -> SerialTitle:
    s = get_title(db, serial_id)
    if s.active != active:
        s.active = active
        audit.record(
            db, actor=actor, action="serial.active", entity="serial", entity_id=s.id, detail={"active": active}, ip=ip
        )
        db.commit()
    return s


def _issue(db: Session, issue_id: int) -> SerialIssue:
    i = db.get(SerialIssue, issue_id)
    if i is None:
        raise SerialError(404, "No such issue")
    return i


def receive_issue(
    db: Session, issue_id: int, *, received_on: date | None, book_in: bool, actor: str, ip: str = ""
) -> dict:
    """Mark an issue as arrived. With `book_in`, a copy is added to the catalogue (ready to tag and lend)."""
    issue = _issue(db, issue_id)
    if issue.status == "received":
        raise SerialError(409, f"Issue {issue.label} was already received")
    s = get_title(db, issue.serial_id)
    day = received_on or date.today()
    if day > date.today() + timedelta(days=1) or day < date(2000, 1, 1):
        raise SerialError(422, "The date received looks wrong")
    item = None
    if book_in:
        try:
            item = catalogue.create_item(
                db,
                {
                    "title": f"{s.title} {issue.label}", "author": "", "isbn": "", "item_type": "serial",
                    "accession_no": f"SER{s.id:03d}-{issue.label}", "location": s.location,
                    "acquired_on": day.isoformat(),
                },
                actor=actor,
                ip=ip,
                commit=False,
            )  # fmt: skip
        except catalogue.CatalogueError as exc:
            db.rollback()
            raise SerialError(exc.status, exc.message) from exc
        issue.item_id = item.id
    issue.status, issue.received_on = "received", day
    audit.record(
        db, actor=actor, action="serial.receive", entity="serial", entity_id=s.id,
        detail={"issue": issue.label, "booked_in": bool(item)}, ip=ip,
    )  # fmt: skip
    db.commit()
    return {"issue": issue_out(issue), "item_id": item.id if item else None}


def mark(db: Session, issue_id: int, status: str, note: str, *, actor: str, ip: str = "") -> dict:
    """Claim a late issue from the supplier, or give it up as missing."""
    if status not in ("claimed", "missing"):
        raise SerialError(422, "An issue can be claimed or marked missing")
    issue = _issue(db, issue_id)
    if issue.status not in ("expected", "claimed"):
        raise SerialError(409, f"Issue {issue.label} is {issue.status}; it cannot be {status}")
    issue.status, issue.note = status, (note or "").strip()[:200]
    audit.record(
        db,
        actor=actor,
        action=f"serial.{status}",
        entity="serial",
        entity_id=issue.serial_id,
        detail={"issue": issue.label},
        ip=ip,
    )
    db.commit()
    return issue_out(issue)


def late_issues(db: Session, today: date | None = None) -> list[dict]:
    """Issues that should have arrived more than a week ago (still expected or already claimed)."""
    today = today or date.today()
    rows = db.execute(
        select(SerialIssue, SerialTitle)
        .join(SerialTitle, SerialTitle.id == SerialIssue.serial_id)
        .where(
            SerialIssue.status.in_(("expected", "claimed")),
            SerialIssue.expected_on < today - timedelta(days=LATE_AFTER_DAYS),
            SerialTitle.active.is_(True),
        )
        .order_by(SerialIssue.expected_on)
    ).all()
    return [
        {**issue_out(i, today), "title": s.title, "supplier": s.supplier, "days_late": (today - i.expected_on).days}
        for i, s in rows
    ]


def counts(db: Session) -> dict:
    return {
        "titles": db.scalar(select(func.count(SerialTitle.id))),
        "late": len(late_issues(db)),
    }
