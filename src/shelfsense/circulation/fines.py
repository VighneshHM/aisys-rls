"""Fines: charged when an overdue item comes back; paid or waived by staff; every change is a ledger row.

Rule (configured under [circulation]), applied when an overdue item is returned or renewed:
    days late        = whole 24-hour periods between the due time and the return time
    billable days    = days late minus `grace_days` (never below zero)
    fine             = billable days x `fine_per_day_cents`, never more than `max_fine_per_loan_cents`
A renewal restarts the clock, so it charges the fine so far first (renewing cannot erase a fine). While an item is
still out the fine so far is *shown* but not yet added to the balance.

The member's balance (`members.fines_cents`) is kept in step with the append-only `fine_entries` ledger inside the same
transaction: balance = opening + charges - payments - waivers. Every amount is traceable to a person and a reason.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.circulation.connector import CirculationConfig
from shelfsense.models import FineEntry, Loan, Member
from shelfsense.timeutil import aware, iso

ADDS = ("opening", "charge")  # raise the balance
REDUCES = ("payment", "waiver")  # lower it


class FineError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def assess(due_at: datetime, end: datetime, config: CirculationConfig) -> dict:
    """What an item due at `due_at` and back (or still out) at `end` costs under the configured rule."""
    seconds = (aware(end) - aware(due_at)).total_seconds()
    late = max(0, int(seconds // 86400))
    billable = max(0, late - config.grace_days)
    raw = billable * config.fine_per_day_cents
    amount = min(raw, config.max_fine_per_loan_cents)
    return {
        "days_late": late,
        "grace_days": config.grace_days,
        "billable_days": billable,
        "per_day_cents": config.fine_per_day_cents,
        "amount_cents": amount,
        "capped": raw > amount,
    }


def entry_out(e: FineEntry) -> dict:
    return {
        "id": e.id,
        "ts": iso(e.ts),
        "kind": e.kind,
        "amount_cents": e.amount_cents,
        "note": e.note,
        "actor": e.actor,
        "loan_id": e.loan_id,
        "detail": e.detail,
    }


def _add(db: Session, member: Member, kind: str, amount: int, note: str, actor: str, loan_id=None, detail=None):
    sign = 1 if kind in ADDS else -1
    member.fines_cents += sign * amount
    entry = FineEntry(
        member_id=member.id,
        loan_id=loan_id,
        kind=kind,
        amount_cents=amount,
        note=note,
        actor=actor,
        detail=detail or {},
    )
    db.add(entry)
    return entry


def charge_overdue(db: Session, loan: Loan, now: datetime, config: CirculationConfig, actor: str) -> dict | None:
    """Add a charge for the lateness so far (at a return or a renewal) to the caller's transaction, or None."""
    a = assess(loan.due_at, now, config)
    if a["amount_cents"] <= 0:
        return None
    note = (
        f"{a['billable_days']} day(s) late after {a['grace_days']} grace, at {a['per_day_cents'] / 100:.2f} a day"
        + (", capped" if a["capped"] else "")
    )
    _add(db, loan.member, "charge", a["amount_cents"], note, actor, loan.id, a)
    return {**a, "note": note, "balance_cents": loan.member.fines_cents}


def _reduce(db: Session, member: Member, kind: str, amount_cents: int, note: str, actor: str, ip: str) -> dict:
    if amount_cents <= 0:
        raise FineError(422, "bad_amount", "The amount must be more than zero")
    if amount_cents > member.fines_cents:
        raise FineError(
            409, "over_balance", f"{member.name} owes {member.fines_cents / 100:.2f}, which is less than that"
        )
    _add(db, member, kind, amount_cents, note, actor)
    audit.record(
        db,
        actor=actor,
        action=f"fine.{kind}",
        entity="member",
        entity_id=member.member_no,
        detail={"amount_cents": amount_cents, "note": note, "balance_cents": member.fines_cents},
        ip=ip,
    )
    db.commit()
    return {"balance_cents": member.fines_cents}


def pay(db: Session, member: Member, amount_cents: int, note: str, *, actor: str, ip: str = "") -> dict:
    return _reduce(db, member, "payment", amount_cents, note.strip(), actor, ip)


def waive(db: Session, member: Member, amount_cents: int, reason: str, *, actor: str, ip: str = "") -> dict:
    if not reason.strip():
        raise FineError(422, "reason_required", "Say why the fine is being waived")
    return _reduce(db, member, "waiver", amount_cents, reason.strip(), actor, ip)


def ledger_balance(db: Session, member_id: int) -> int:
    """The balance as the ledger says it (used to prove the stored balance never drifts)."""
    total = func.sum(case((FineEntry.kind.in_(ADDS), FineEntry.amount_cents), else_=-FineEntry.amount_cents))
    return db.scalar(select(total).where(FineEntry.member_id == member_id)) or 0


def recent(db: Session, member_id: int, limit: int = 8) -> list[dict]:
    rows = db.scalars(
        select(FineEntry).where(FineEntry.member_id == member_id).order_by(FineEntry.id.desc()).limit(limit)
    )
    return [entry_out(e) for e in rows]
