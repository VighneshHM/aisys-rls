"""Viewing every member, and removing, restoring or erasing one (the Patrons page).

Removing never deletes a row: a removed member keeps their loans, fines and history, but cannot borrow, their card is
switched off, and they drop out of the normal lists. It is refused while they still have items out or owe a fine, so
nothing is lost track of. Erasing is the second, irreversible step for a removed member: it clears the personal details
(name, e-mail, phone, card numbers, the text of reminder messages) and keeps the numbers, so reports still add up.

How "removed" is stored: `removed_at` is set and `status` is `blocked` (the database rule allows only active and
blocked, and rebuilding the members table is not worth the risk), so any older code path refuses a removed member too.
The audit log and the circulation history are append-only by design, so they never hold a member's name, e-mail or
phone: they record member numbers (`service.event_text` swaps a name for the number before a message is kept).
With a real ILMS the members live there; removing or erasing here does not touch the ILMS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.auth.security import card_hint
from shelfsense.circulation.service import CircError, find_member, member_status
from shelfsense.models import Card, Loan, Member, Notification, utcnow
from shelfsense.timeutil import iso

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

STATUSES = ("active", "blocked", "removed")
MAX_PAGE = 100
REMOVED_NOTE = "Removed"


def _state(status: str):
    """The WHERE for one member state (removed members are stored as blocked, so they must be told apart)."""
    if status == "removed":
        return Member.removed_at.is_not(None)
    return and_(Member.status == status, Member.removed_at.is_(None))


def _filters(q: str, status: str, loans: bool, fines: bool) -> list:
    """The WHERE parts shared by the list and its total. Removed members only show when asked for."""
    conds: list = [_state(status)] if status in STATUSES else [Member.removed_at.is_(None)]
    text = q.strip().lower().replace("%", "").replace("_", "")
    if text:
        like = f"%{text}%"
        conds.append(
            or_(
                func.lower(Member.name).like(like),
                func.lower(Member.member_no).like(like),
                func.lower(Member.email).like(like),
                func.lower(Member.phone).like(like),
            )
        )
    if loans:
        conds.append(
            select(func.count(Loan.id)).where(Loan.member_id == Member.id, Loan.returned_at.is_(None)).scalar_subquery()
            > 0
        )
    if fines:
        conds.append(Member.fines_cents > 0)
    return conds


def list_members(
    db: Session,
    *,
    q: str = "",
    status: str = "",
    loans: bool = False,
    fines: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Every member, a page at a time, with what staff need at a glance (loans out, fines, card, contact)."""
    limit = min(max(int(limit), 1), MAX_PAGE)
    offset = max(int(offset), 0)
    conds = _filters(q, status, loans, fines)
    total = db.scalar(select(func.count(Member.id)).where(*conds)) or 0
    rows = list(
        db.scalars(select(Member).where(*conds).order_by(Member.name, Member.member_no).limit(limit).offset(offset))
    )
    open_by_id = dict(
        db.execute(
            select(Loan.member_id, func.count(Loan.id))
            .where(Loan.returned_at.is_(None), Loan.member_id.in_([m.id for m in rows]))
            .group_by(Loan.member_id)
        ).all()
    )
    cards = dict(
        db.execute(
            select(Card.member_ref, Card.uid).where(
                Card.kind == "patron", Card.active, Card.member_ref.in_([m.member_no for m in rows])
            )
        ).all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "members": [
            {
                "member_no": m.member_no,
                "name": m.name,
                "status": member_status(m),
                "block_reason": "" if m.removed_at is not None else m.block_reason,
                "open_loans": open_by_id.get(m.id, 0),
                "fines_cents": m.fines_cents,
                "email": m.email,
                "phone": m.phone,
                "card": card_hint(cards[m.member_no]) if m.member_no in cards else None,
                "joined": iso(m.created_at),
                "removed": (
                    {
                        "at": iso(m.removed_at),
                        "by": m.removed_by,
                        "reason": m.removed_reason,
                        "erased": m.erased_at is not None,
                    }
                    if m.removed_at is not None
                    else None
                ),
            }
            for m in rows
        ],
    }


def counts(db: Session) -> dict:
    """How many members in each state, for the filter buttons."""

    def n(*conds) -> int:
        return db.scalar(select(func.count(Member.id)).where(*conds)) or 0

    out = {s: n(_state(s)) for s in STATUSES}
    out["all"] = out["active"] + out["blocked"]
    out["with_loans"] = (
        db.scalar(select(func.count(func.distinct(Loan.member_id))).where(Loan.returned_at.is_(None))) or 0
    )
    out["owing"] = n(Member.removed_at.is_(None), Member.fines_cents > 0)
    return out


def remove_member(db: Session, member_no: str, reason: str, *, actor: str, ip: str = "") -> Member:
    """Remove a member: history kept, borrowing stopped, card switched off. Refused with loans out or a fine owed."""
    member = find_member(db, member_no)
    if member.removed_at is not None:
        raise CircError(409, "already_removed", f"{member.name} has already been removed")
    reason = " ".join((reason or "").split())[:200]
    # Claim the member first, in one atomic update that only matches a member not yet removed. That takes the database's
    # write lock, so the checks below see everything committed so far (a loan written a moment ago, a second remover).
    claimed = db.execute(
        update(Member)
        .where(Member.id == member.id, Member.removed_at.is_(None))
        .values(
            status_before_removal=Member.status,
            status="blocked",  # blocked underneath: nothing can lend to them
            block_reason=REMOVED_NOTE,
            removed_at=utcnow(),
            removed_by=actor,
            removed_reason=reason,
        )
    )
    if cast("CursorResult[Any]", claimed).rowcount != 1:
        db.rollback()
        raise CircError(409, "already_removed", f"{member.name} has already been removed")
    db.refresh(member)
    out = db.scalar(select(func.count(Loan.id)).where(Loan.member_id == member.id, Loan.returned_at.is_(None))) or 0
    if out or member.fines_cents > 0:
        name, owed = member.name, member.fines_cents
        db.rollback()  # undo the claim: nothing changes
        if out:
            what = "them" if out != 1 else "it"
            raise CircError(
                409, "has_loans", f"{name} still has {out} item{'s' if out != 1 else ''} on loan: return {what} first"
            )
        raise CircError(409, "owes_fines", f"{name} owes {owed / 100:.2f}: record the payment or waive the fine first")
    cards = list(
        db.scalars(select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no, Card.active))
    )
    for card in cards:
        card.active = False  # a removed member's card is switched off, like a lost one
    waiting = list(
        db.scalars(select(Notification).where(Notification.member_id == member.id, Notification.status == "queued"))
    )
    for note in waiting:
        note.status = "cancelled"
        note.last_error = "member removed"
    audit.record(
        db,
        actor=actor,
        action="member.remove",
        entity="member",
        entity_id=member.member_no,
        detail={"reason": reason, "cards_off": len(cards), "reminders_cancelled": len(waiting)},
        ip=ip,
    )
    db.commit()
    return member


def restore_member(db: Session, member_no: str, *, actor: str, ip: str = "") -> Member:
    """Bring a removed member back as they were (active or blocked). Their old card stays off: issue a new one."""
    member = find_member(db, member_no)
    if member.removed_at is None:
        raise CircError(409, "not_removed", f"{member.name} has not been removed")
    if member.erased_at is not None:
        raise CircError(
            409, "erased", "This member's details were erased, so they cannot be restored: add them as a new member"
        )
    member.status = member.status_before_removal or "active"
    member.block_reason = "" if member.status == "active" else member.block_reason
    member.removed_at, member.removed_by, member.removed_reason, member.status_before_removal = None, "", "", ""
    if member.status == "blocked" and member.block_reason == REMOVED_NOTE:
        member.block_reason = ""
    audit.record(db, actor=actor, action="member.restore", entity="member", entity_id=member.member_no, ip=ip)
    db.commit()
    return member


def erase_member(db: Session, member_no: str, confirm: str, *, actor: str, ip: str = "") -> Member:
    """Erase a removed member's personal details for good. Needs the member number typed back as confirmation."""
    member = find_member(db, member_no)
    if member.removed_at is None:
        raise CircError(409, "not_removed", "Only a removed member can be erased: remove them first")
    if member.erased_at is not None:
        raise CircError(409, "already_erased", "This member's personal details were already erased")
    if confirm.strip().upper() != member.member_no:
        raise CircError(422, "confirm_mismatch", f"Type the member number {member.member_no} to confirm")
    placeholder = f"Removed member {member.member_no}"
    member.name, member.email, member.phone, member.reminders_on = placeholder, "", "", False
    member.erased_at = utcnow()
    cards = list(db.scalars(select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no)))
    for card in cards:
        db.delete(card)  # the card numbers become free to use again
    scrubbed = 0
    for note in db.scalars(select(Notification).where(Notification.member_id == member.id)):
        note.recipient, note.subject, note.body = "", "(erased)", "(erased)"
        scrubbed += 1
    audit.record(
        db,
        actor=actor,
        action="member.erase",
        entity="member",
        entity_id=member.member_no,
        detail={"cards_deleted": len(cards), "messages_scrubbed": scrubbed},
        ip=ip,
    )
    db.commit()
    return member
