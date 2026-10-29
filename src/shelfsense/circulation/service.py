"""Circulation workflow: check out, renew, check in, due-date enquiry. One function per staff action.

Order of every transaction (same philosophy as tagging):
    1. local checks that need only our own records (reference item, blocked member, fines over the limit, item state)
    2. the ILMS connector, which owns the loan policy (loan period, renewal and loan limits) and the final say
    3. device: switch the tag's security bit (off on check-out, on again at check-in); best effort, never blocks a loan
    4. one database commit: loan, item status and the audit-trail row together; if it fails, step 3 is undone
Every attempt - done, refused or failed - leaves a row in `circ_events`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, NoReturn, cast

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.circulation import fines
from shelfsense.circulation.connector import (
    CirculationConfig,
    CirculationConnector,
    ConnectorResult,
    ItemView,
    MemberView,
)
from shelfsense.models import Card, CircEvent, Item, Loan, Member, Notification, RfidTag, utcnow
from shelfsense.rfid.devices import DeviceError, DeviceUnavailable, StaffReader
from shelfsense.rfid.events import normalize_tag_uid
from shelfsense.rfid.registry import DeviceRegistry
from shelfsense.timeutil import aware, iso

log = logging.getLogger("shelfsense.circulation")
LENDABLE_STATUSES = {"available", "on_loan"}  # anything else (damaged, lost, withdrawn) cannot be lent


class CircError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ lookups and presentation
def member_status(member: Member) -> str:
    """active | blocked | removed: what staff see (a removed member is stored as blocked with `removed_at` set)."""
    return "removed" if member.removed_at is not None else member.status


def event_text(message: str, member: Member | None) -> str:
    """The text kept in the history: the member number stands in for the name. The history can never be edited, so it
    must not hold personal data (it already records who it was about by member number)."""
    if member is None or not member.name:
        return message
    return message.replace(member.name, member.member_no)


def find_member(db: Session, member_no: str) -> Member:
    member = db.scalar(select(Member).where(Member.member_no == member_no.strip().upper()))
    if member is None:
        raise CircError(404, "no_such_member", f"No member with number '{member_no}'")
    return member


def identify(db: Session, uid: str, *, actor: str, ip: str = "") -> Member:
    """A patron card identifies a member. It never logs anyone in (staff cards do that, elsewhere)."""
    card = db.scalar(select(Card).where(Card.uid == uid.strip().upper()))
    if card is None or card.kind != "patron" or not card.member_ref:
        raise CircError(404, "unknown_card", "This is not a registered patron card")
    member = find_member(db, card.member_ref)
    if member.removed_at is not None:
        raise CircError(409, "member_removed", f"{member.name} has been removed as a member")
    if not card.active:
        raise CircError(409, "card_inactive", "This card is no longer active (it was replaced or withdrawn)")
    audit.record(db, actor=actor, action="member.identify", entity="member", entity_id=member.member_no, ip=ip)
    db.commit()
    return member


def resolve_item(db: Session, ref: str) -> Item:
    """An item can be named by accession number, barcode, or the UID of its active tag (what a reader sees)."""
    ref = ref.strip()
    if not ref:
        raise CircError(422, "no_item", "Give an accession number, barcode or tag")
    key = ref.lower()
    item = db.scalar(select(Item).where(or_(func.lower(Item.accession_no) == key, func.lower(Item.barcode) == key)))
    if item is None:
        item = db.scalar(
            select(Item)
            .join(RfidTag, RfidTag.item_id == Item.id)
            .where(RfidTag.tag_uid == normalize_tag_uid(ref), RfidTag.status == "active")
        )
    if item is None:
        raise CircError(404, "no_such_item", f"No item matches '{ref}'")
    return item


def open_loan(db: Session, item_id: int) -> Loan | None:
    return db.scalar(select(Loan).where(Loan.item_id == item_id, Loan.returned_at.is_(None)))


def open_count(db: Session, member_id: int) -> int:
    return (
        db.scalar(select(func.count()).select_from(Loan).where(Loan.member_id == member_id, Loan.returned_at.is_(None)))
        or 0
    )


def loan_out(loan: Loan, now: datetime | None = None, config: CirculationConfig | None = None) -> dict:
    now = now or utcnow()
    overdue = loan.returned_at is None and aware(loan.due_at) < aware(now)
    accruing = fines.assess(loan.due_at, now, config) if overdue and config else None
    return {
        "id": loan.id,
        "member_no": loan.member.member_no,
        "member_name": loan.member.name,
        "item_id": loan.item_id,
        "accession_no": loan.item.accession_no,
        "title": loan.item.bib.title,
        "out_at": iso(loan.out_at),
        "due_at": iso(loan.due_at),
        "renewals": loan.renewals,
        "returned_at": iso(loan.returned_at),
        "overdue": overdue,
        "days_overdue": (aware(now) - aware(loan.due_at)).days if overdue else 0,
        "fine_so_far_cents": accruing["amount_cents"] if accruing else 0,
        "out_by": loan.out_by,
        "returned_by": loan.returned_by,
    }


def _recent_reminders(db: Session, member: Member, limit: int = 6) -> list[dict]:
    rows = db.scalars(
        select(Notification).where(Notification.member_id == member.id).order_by(Notification.id.desc()).limit(limit)
    )
    return [
        {
            "id": n.id,
            "kind": n.kind,
            "channel": n.channel,
            "status": n.status,
            "due_on": n.due_on,
            "created_at": iso(n.created_at),
            "sent_at": iso(n.sent_at),
            "note": n.last_error,
        }
        for n in rows
    ]


def member_out(db: Session, member: Member, config: CirculationConfig, *, with_loans: bool = True) -> dict:
    loans = list(
        db.scalars(select(Loan).where(Loan.member_id == member.id, Loan.returned_at.is_(None)).order_by(Loan.due_at))
    )
    cards = list(
        db.scalars(
            select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no).order_by(Card.id.desc())
        )
    )
    current = next((c for c in cards if c.active), None)
    return {
        "member_no": member.member_no,
        "name": member.name,
        "card": {"uid": current.uid, "issued_at": iso(current.issued_at)} if current else None,
        "cards_withdrawn": sum(1 for c in cards if not c.active),
        "status": member_status(member),
        "block_reason": "" if member.removed_at is not None else member.block_reason,
        "removed": (
            {"at": iso(member.removed_at), "by": member.removed_by, "reason": member.removed_reason}
            if member.removed_at is not None
            else None
        ),
        "erased": member.erased_at is not None,
        "contact": {"email": member.email, "phone": member.phone, "reminders_on": member.reminders_on},
        "reminders": _recent_reminders(db, member),
        "fines_cents": member.fines_cents,
        "fine_limit_cents": config.fine_limit_cents,
        "fines_over_limit": member.fines_cents > config.fine_limit_cents,
        "open_loans": len(loans),
        "max_loans": config.max_loans,
        "can_borrow": member.status == "active" and member.fines_cents <= config.fine_limit_cents,
        "loans": [loan_out(x, config=config) for x in loans] if with_loans else [],
        "fine_entries": fines.recent(db, member.id) if with_loans else [],
    }


def set_blocked(db: Session, member_no: str, blocked: bool, reason: str, *, actor: str, ip: str = "") -> Member:
    member = find_member(db, member_no)
    if member.removed_at is not None:
        raise CircError(409, "member_removed", f"{member.name} has been removed: restore them before changing anything")
    member.status = "blocked" if blocked else "active"
    member.block_reason = reason.strip() if blocked else ""
    audit.record(
        db,
        actor=actor,
        action="member.block" if blocked else "member.unblock",
        entity="member",
        entity_id=member.member_no,
        detail={"reason": member.block_reason},
        ip=ip,
    )
    db.commit()
    return member


# ------------------------------------------------------------------ internals
def _txn_id() -> str:
    return "T" + uuid.uuid4().hex[:24]


def _member_view(db: Session, member: Member) -> MemberView:
    return MemberView(
        member.member_no, member.status, member.fines_cents, open_count(db, member.id), member.block_reason
    )


def _item_view(item: Item, loan: Loan | None) -> ItemView:
    return ItemView(item.accession_no, loan is not None, loan.renewals if loan else 0, loan.due_at if loan else None)


def _refuse(
    db: Session,
    *,
    action: str,
    code: str,
    message: str,
    status: int,
    source: str,
    txn_id: str,
    protocol: str,
    member: Member | None,
    item: Item | None,
    loan: Loan | None,
    actor: str,
    outcome: str = "refused",
) -> NoReturn:
    """Record the attempt that did not go ahead, then raise. Nothing else in the database changes."""
    db.rollback()
    db.add(
        CircEvent(
            action=action,
            outcome=outcome,
            code=code,
            message=event_text(message, member),
            protocol=protocol,
            txn_id=txn_id,
            member_id=member.id if member else None,
            item_id=item.id if item else None,
            loan_id=loan.id if loan else None,
            actor=actor,
            detail={
                "source": source,
                "accession_no": item.accession_no if item else "",
                "member_no": member.member_no if member else "",
            },
        )
    )
    db.commit()
    raise CircError(503 if code == "ilms_unavailable" else 409, code, message)


def _local_refusal(member: Member | None, item: Item, loan: Loan | None, config: CirculationConfig) -> tuple[str, str]:
    """The first rule that stops a loan, from our own records, or ('', '') if none does."""
    if item.is_reference or item.item_type == "reference":
        return "reference_item", f"'{item.bib.title}' is a reference item and cannot be borrowed"
    if item.status not in LENDABLE_STATUSES:
        return "item_unavailable", f"This item is marked {item.status} and cannot be lent"
    if loan is not None:
        return "item_on_loan", "This item is already on loan"
    if member is not None:
        if member.removed_at is not None:
            return "member_removed", f"{member.name} has been removed as a member"
        if member.status != "active":
            why = f": {member.block_reason}" if member.block_reason else ""
            return "patron_blocked", f"{member.name} is blocked{why}"
        if member.fines_cents > config.fine_limit_cents:
            return "fines_exceeded", (
                f"{member.name} owes {member.fines_cents / 100:.2f}, over the limit of "
                f"{config.fine_limit_cents / 100:.2f}"
            )
    return "", ""


# ------------------------------------------------------------------ the security bit on the tag
def _security(db: Session, devices: DeviceRegistry, item: Item, *, armed: bool, reader_id: str) -> dict:
    """Switch the item's tag security bit on the reader. Always returns a status; never raises."""
    verb = "switched back on" if armed else "switched off"
    tag = db.scalar(select(RfidTag).where(RfidTag.item_id == item.id, RfidTag.status == "active"))
    if tag is None:
        return {"status": "no_tag", "ok": True, "message": "This item has no active tag, so there was no security bit."}
    reader = cast("StaffReader | None", devices.get(reader_id))
    todo = f"Have the tag {verb} before the item passes the gate."
    if reader is None:
        return {"status": "no_reader", "ok": False, "message": f"Reader '{reader_id}' is not configured. {todo}"}
    try:
        if tag.tag_uid not in {t.uid for t in reader.read_field()}:
            return {"status": "not_on_reader", "ok": False, "message": f"The item's tag was not on the reader. {todo}"}
        reader.set_security(tag.tag_uid, armed)
    except DeviceUnavailable:
        return {"status": "reader_offline", "ok": False, "message": f"The reader is offline. {todo}"}
    except DeviceError as exc:
        return {
            "status": "device_error",
            "ok": False,
            "message": f"The reader could not change the tag ({exc}). {todo}",
        }
    tag.security_armed = armed
    return {"status": "armed" if armed else "disarmed", "ok": True, "message": f"Security bit {verb}."}


def _beep(devices: DeviceRegistry, reader_id: str, pattern: str) -> None:
    reader = cast("StaffReader | None", devices.get(reader_id))
    if reader is not None:
        try:
            reader.indicate(pattern)
        except DeviceError:
            log.warning("could not signal '%s' on reader %s", pattern, reader_id)


# ------------------------------------------------------------------ the three actions
def _finish(
    db: Session,
    devices: DeviceRegistry,
    *,
    action: str,
    result: ConnectorResult,
    txn_id: str,
    member: Member,
    item: Item,
    loan: Loan,
    actor: str,
    reader_id: str,
    armed: bool | None,
    extra: dict | None = None,
) -> dict:
    """Steps 3 and 4: security bit, then the single commit that makes the transaction real."""
    if armed is None:  # a renewal does not touch the tag
        security = {"status": "unchanged", "ok": True, "message": ""}
    else:
        security = _security(db, devices, item, armed=armed, reader_id=reader_id)
    db.flush()
    db.add(
        CircEvent(
            action=action,
            outcome="ok",
            code=result.code,
            message=event_text(result.message, member),
            protocol=result.protocol,
            txn_id=txn_id,
            member_id=member.id,
            item_id=item.id,
            loan_id=loan.id,
            actor=actor,
            detail={
                "source": "ilms",
                "replayed": result.replayed,
                "accession_no": item.accession_no,
                "member_no": member.member_no,
                "due_at": iso(loan.due_at),
                "security": security,
                **(extra or {}),
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:  # e.g. two desks lent the same copy at once: the database refuses the second
        db.rollback()
        if armed is not None and security["status"] in ("armed", "disarmed"):
            _security(db, devices, item, armed=not armed, reader_id=reader_id)  # undo the device change
            db.rollback()
        raise CircError(409, "item_on_loan", "Another desk just changed this item; please check it again") from exc
    _beep(devices, reader_id, "ok" if security["ok"] else "warning")
    return {
        "ok": True,
        "action": action,
        "code": result.code,
        "message": result.message,
        "protocol": result.protocol,
        "txn_id": txn_id,
        "replayed": result.replayed,
        "security": security,
        "loan": loan_out(loan),
    }


def checkout(
    db: Session,
    connector: CirculationConnector,
    devices: DeviceRegistry,
    config: CirculationConfig,
    *,
    member_no: str,
    item_ref: str,
    actor: str,
    reader_id: str = "staff-1",
) -> dict:
    member, item = find_member(db, member_no), resolve_item(db, item_ref)
    loan, txn_id, now = open_loan(db, item.id), _txn_id(), utcnow()
    common: dict[str, Any] = dict(
        action="checkout", txn_id=txn_id, protocol=connector.protocol, member=member, item=item, loan=loan, actor=actor
    )
    code, message = _local_refusal(member, item, loan, config)
    if code:
        _refuse(db, code=code, message=message, status=409, source="local", **common)
    result = connector.checkout(txn_id, _member_view(db, member), _item_view(item, None), now=now)
    if not result.ok:
        outcome = "failed" if result.code == "ilms_unavailable" else "refused"
        _refuse(db, code=result.code, message=result.message, status=409, source="ilms", outcome=outcome, **common)
    db.refresh(member)  # the ILMS call can take seconds: was the member removed meanwhile?
    if member.removed_at is not None:
        _undo_ilms_checkout(connector, item, result.due_at, now)
        _refuse(
            db,
            code="member_removed",
            message=f"{member.name} was removed while this was being recorded, so nothing was lent",
            status=409,
            source="local",
            **common,
        )
    new = Loan(member_id=member.id, item_id=item.id, out_at=now, due_at=result.due_at, out_by=actor)
    db.add(new)
    item.status = "on_loan"
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise CircError(409, "item_on_loan", "This item is already on loan") from exc
    return _finish(
        db,
        devices,
        action="checkout",
        result=result,
        txn_id=txn_id,
        member=member,
        item=item,
        loan=new,
        actor=actor,
        reader_id=reader_id,
        armed=False,
    )


def _undo_ilms_checkout(connector: CirculationConnector, item: Item, due_at: datetime | None, now: datetime) -> None:
    """Take back a loan the ILMS accepted but is not going to be recorded here. Best effort; a failure is logged."""
    try:
        undone = connector.checkin(_txn_id(), ItemView(item.accession_no, True, 0, due_at), now=now)
        if not undone.ok:
            log.warning("could not take back the ILMS loan of %s: %s", item.accession_no, undone.message)
    except Exception:  # noqa: BLE001 - never let the undo hide the reason the loan was refused
        log.exception("could not take back the ILMS loan of %s", item.accession_no)


def renew(
    db: Session,
    connector: CirculationConnector,
    devices: DeviceRegistry,
    config: CirculationConfig,
    *,
    item_ref: str,
    actor: str,
    reader_id: str = "staff-1",
) -> dict:
    item = resolve_item(db, item_ref)
    loan, txn_id, now = open_loan(db, item.id), _txn_id(), utcnow()
    member = loan.member if loan else None
    common: dict[str, Any] = dict(
        action="renew", txn_id=txn_id, protocol=connector.protocol, member=member, item=item, loan=loan, actor=actor
    )
    if loan is None:
        _refuse(
            db,
            code="not_on_loan",
            message="This item is not on loan, so there is nothing to renew",
            status=409,
            source="local",
            **common,
        )
    member = loan.member
    code, message = _local_refusal(
        member, item, None, config
    )  # blocked member / fines; the item IS on loan, which is fine
    if code in ("patron_blocked", "fines_exceeded"):
        _refuse(db, code=code, message=message, status=409, source="local", **common)
    result = connector.renew(txn_id, _member_view(db, member), _item_view(item, loan), now=now)
    if not result.ok:
        outcome = "failed" if result.code == "ilms_unavailable" else "refused"
        _refuse(db, code=result.code, message=result.message, status=409, source="ilms", outcome=outcome, **common)
    old_due = loan.due_at
    # renewing restarts the clock, so any fine earned so far is charged now (otherwise renewing would erase it)
    fine = fines.charge_overdue(db, loan, now, config, actor)
    assert result.due_at is not None  # an accepted renewal always carries the new due date
    loan.due_at, loan.renewals = result.due_at, loan.renewals + 1
    out = _finish(
        db,
        devices,
        action="renew",
        result=result,
        txn_id=txn_id,
        member=member,
        item=item,
        loan=loan,
        actor=actor,
        reader_id=reader_id,
        armed=None,
        extra={"previous_due_at": iso(old_due), "fine_cents": fine["amount_cents"] if fine else 0},
    )
    out["fine"] = fine
    return out


def checkin(
    db: Session,
    connector: CirculationConnector,
    devices: DeviceRegistry,
    config: CirculationConfig,
    *,
    item_ref: str,
    actor: str,
    reader_id: str = "staff-1",
) -> dict:
    item = resolve_item(db, item_ref)
    loan, txn_id, now = open_loan(db, item.id), _txn_id(), utcnow()
    member = loan.member if loan else None
    common: dict[str, Any] = dict(
        action="checkin", txn_id=txn_id, protocol=connector.protocol, member=member, item=item, loan=loan, actor=actor
    )
    if loan is None:
        _refuse(
            db,
            code="not_on_loan",
            message="This item is not on loan, so it cannot be checked in",
            status=409,
            source="local",
            **common,
        )
    member = loan.member
    result = connector.checkin(txn_id, _item_view(item, loan), now=now)
    reconciled = False
    if not result.ok and result.code == "not_on_loan":
        # The library system already has this item in (it was returned at another desk there): the state the clerk
        # wants is the state it is in, so close the local loan too instead of leaving it open for ever.
        reconciled = True
        result = replace(result, ok=True, code="ok", message="The library system already had this item checked in")
    if not result.ok:
        outcome = "failed" if result.code == "ilms_unavailable" else "refused"
        _refuse(db, code=result.code, message=result.message, status=409, source="ilms", outcome=outcome, **common)
    was_overdue = aware(loan.due_at) < aware(now)
    loan.returned_at, loan.returned_by = now, actor
    if item.status == "on_loan":
        item.status = "available"
    fine = fines.charge_overdue(db, loan, now, config, actor)  # joins the same commit as the return
    out = _finish(
        db,
        devices,
        action="checkin",
        result=result,
        txn_id=txn_id,
        member=member,
        item=item,
        loan=loan,
        actor=actor,
        reader_id=reader_id,
        armed=True,
        extra={"was_overdue": was_overdue, "fine_cents": fine["amount_cents"] if fine else 0, "reconciled": reconciled},
    )
    out["fine"] = fine
    out["was_overdue"] = was_overdue
    return out


def due_date(db: Session, connector: CirculationConnector, item_ref: str) -> dict:
    """Due-date enquiry: asks the ILMS (read only, so it is not written to the transaction history)."""
    item = resolve_item(db, item_ref)
    loan = open_loan(db, item.id)
    if loan is None:
        return {"on_loan": False, "accession_no": item.accession_no, "title": item.bib.title, "message": "Not on loan"}
    result = connector.due_date(_txn_id(), _item_view(item, loan))
    return {
        "on_loan": True,
        "accession_no": item.accession_no,
        "title": item.bib.title,
        "ok": result.ok,
        "message": result.message,
        "protocol": result.protocol,
        "due_at": iso(result.due_at),
        "member_no": loan.member.member_no,
        "renewals": loan.renewals,
    }


# ------------------------------------------------------------------ history
def list_loans(
    db: Session,
    *,
    member_no: str = "",
    open_only: bool = True,
    limit: int = 25,
    offset: int = 0,
    config: CirculationConfig | None = None,
) -> dict:
    q = select(Loan).join(Member, Loan.member_id == Member.id)
    if member_no:
        q = q.where(Member.member_no == member_no.strip().upper())
    if open_only:
        q = q.where(Loan.returned_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Loan.id.desc()).limit(limit).offset(offset)).unique().all()
    return {"total": total, "loans": [loan_out(x, config=config) for x in rows]}


def event_out(e: CircEvent, member_no: str | None, accession_no: str | None) -> dict:
    return {
        "id": e.id,
        "ts": iso(e.ts),
        "action": e.action,
        "outcome": e.outcome,
        "code": e.code,
        "message": e.message,
        "protocol": e.protocol,
        "txn_id": e.txn_id,
        "member_no": member_no,
        "accession_no": accession_no,
        "actor": e.actor,
        "detail": e.detail,
    }


def list_events(db: Session, *, member_no: str = "", item_ref: str = "", limit: int = 30, offset: int = 0) -> dict:
    q = (
        select(CircEvent, Member.member_no, Item.accession_no)
        .outerjoin(Member, CircEvent.member_id == Member.id)
        .outerjoin(Item, CircEvent.item_id == Item.id)
    )
    if member_no:
        q = q.where(Member.member_no == member_no.strip().upper())
    if item_ref:
        q = q.where(or_(Item.accession_no == item_ref.strip(), Item.barcode == item_ref.strip()))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.order_by(CircEvent.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "events": [event_out(e, m, a) for e, m, a in rows]}
