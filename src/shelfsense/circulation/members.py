"""Members: create them, issue or replace their patron card, find them by name.

A patron card only identifies a member; it never gives a login. Issuing a new card to a member who already has one
(lost, damaged) switches the old card off in the same transaction, so a lost card cannot be used any more.
"""

from __future__ import annotations

import re
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.circulation.service import CircError, find_member, member_status
from shelfsense.models import Card, Member
from shelfsense.notifications.providers import valid_address
from shelfsense.rfid import tagging
from shelfsense.rfid.devices import CardJob, CardPrinter, DeviceError, DeviceUnavailable
from shelfsense.rfid.registry import DeviceRegistry

MEMBER_NO_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")


def next_member_no(db: Session) -> str:
    """M0001, M0002, ...: one more than the highest M-number in use (other formats are left alone)."""
    numbers = [int(m[1:]) for (m,) in db.execute(select(Member.member_no)) if re.fullmatch(r"M\d+", m)]
    return f"M{max(numbers, default=0) + 1:04d}"


def _refuse(status: int, problems: list[dict]) -> CircError:
    first = problems[0]
    return CircError(status, first["code"], "; ".join(p["message"] for p in problems))


def clean_email(value: str) -> str:
    value = (value or "").strip()
    if value and (len(value) > 200 or not valid_address("email", value)):
        raise CircError(422, "bad_email", "That is not an e-mail address like name@example.org")
    return value


def clean_phone(value: str) -> str:
    value = re.sub(r"[\s().-]", "", value or "")
    if value and not valid_address("sms", value):
        raise CircError(422, "bad_phone", "A phone number has 7 to 15 digits, like +441234567890")
    return value


def set_contact(
    db: Session,
    member_no: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    reminders_on: bool | None = None,
    actor: str,
    ip: str = "",
) -> Member:
    """Change where reminders go. A field left out (None) is unchanged; an empty text clears it."""
    member = find_member(db, member_no)
    if member.removed_at is not None:
        raise CircError(409, "member_removed", f"{member.name} has been removed: restore them before changing anything")
    changed: dict[str, str] = {}
    if email is not None:
        member.email = clean_email(email)
        changed["email"] = "set" if member.email else "cleared"
    if phone is not None:
        member.phone = clean_phone(phone)
        changed["phone"] = "set" if member.phone else "cleared"
    if reminders_on is not None:
        member.reminders_on = reminders_on
        changed["reminders"] = "on" if reminders_on else "off"
    if changed:  # the addresses themselves are personal data: the log records only what was done
        audit.record(
            db, actor=actor, action="member.contact", entity="member", entity_id=member.member_no, detail=changed, ip=ip
        )
        db.commit()
    return member


def create_member(
    db: Session,
    *,
    name: str,
    member_no: str = "",
    card_uid: str = "",
    email: str = "",
    phone: str = "",
    actor: str,
    ip: str = "",
) -> Member:
    name = " ".join(name.split())
    if not name:
        raise CircError(422, "name_required", "A member needs a name")
    number = member_no.strip().upper() or next_member_no(db)
    if not MEMBER_NO_RE.match(number):
        raise CircError(422, "bad_member_no", "A member number is letters, digits and dashes, like M0042")
    if db.scalar(select(Member).where(Member.member_no == number)):
        raise CircError(409, "member_exists", f"Member number {number} is already in use")
    uid = tagging.normalize_card_uid(card_uid)
    problems = tagging.card_problems(db, uid) if uid else []
    if problems:
        raise _refuse(409 if problems[0]["code"] == "card_in_use" else 422, problems)
    member = Member(member_no=number, name=name, email=clean_email(email), phone=clean_phone(phone))
    db.add(member)
    db.flush()
    if uid:
        db.add(Card(uid=uid, kind="patron", member_ref=number))
    audit.record(
        db,
        actor=actor,
        action="member.create",
        entity="member",
        entity_id=number,
        detail={"card": bool(uid)},  # a name is personal data and the audit log is append-only: it holds numbers only
        ip=ip,
    )
    db.commit()
    return member


def issue_card(db: Session, member_no: str, card_uid: str, *, actor: str, ip: str = "") -> Member:
    """Give a member a card. Any card they had before is switched off (lost or replaced).

    The member record is validated first, by the check the tagging workflow uses (`tagging.validate_member_tagging`).
    """
    chk = tagging.validate_member_tagging(db, member_no, card_uid)
    if not chk.ok:
        raise _refuse(chk.status_code(), chk.problems)
    member, uid = chk.member, chk.uid
    assert member is not None  # the check passed
    old = list(db.scalars(select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no, Card.active)))
    for card in old:
        card.active = False
    db.add(Card(uid=uid, kind="patron", member_ref=member.member_no))
    audit.record(
        db,
        actor=actor,
        action="member.card_issue",
        entity="member",
        entity_id=member.member_no,
        detail={"replaced": [c.uid for c in old]},
        ip=ip,
    )
    db.commit()
    return member


def search(db: Session, q: str, limit: int = 10, *, include_removed: bool = False) -> list[dict]:
    """Members whose name or number contains `q` (case-insensitive), best matches first. Removed ones are left out."""
    q = q.strip().lower().replace("%", "").replace("_", "")  # no wildcards: they would match everyone
    if not q:
        return []
    like = f"%{q}%"
    hide = () if include_removed else (Member.removed_at.is_(None),)
    rows = list(
        db.scalars(
            select(Member)
            .where(or_(func.lower(Member.name).like(like), func.lower(Member.member_no).like(like)), *hide)
            .order_by(Member.name)
            .limit(limit)
        )
    )
    cards = dict(
        db.execute(
            select(Card.member_ref, Card.uid).where(
                Card.kind == "patron", Card.active, Card.member_ref.in_([m.member_no for m in rows])
            )
        ).all()
    )
    return [
        {
            "member_no": m.member_no,
            "name": m.name,
            "status": member_status(m),
            "fines_cents": m.fines_cents,
            "card": cards.get(m.member_no),
        }
        for m in rows
    ]


def _active_card(db: Session, member: Member) -> Card:
    card = db.scalar(select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no, Card.active))
    if card is None:
        raise CircError(409, "no_card", f"{member.name} has no card yet: issue one first, then print it")
    return card


def card_face(db: Session, member_no: str, library: str) -> dict:
    """What is printed on a member's card: name, number and a barcode of the card number."""
    from shelfsense.labels import code128

    member = find_member(db, member_no)
    card = _active_card(db, member)
    return {
        "library": library,
        "name": member.name,
        "member_no": member.member_no,
        "card_uid": card.uid,
        "modules": code128.encode(card.uid) if code128.encodable(card.uid) else "",
    }


def personalise_card(
    db: Session, devices: DeviceRegistry, member_no: str, library: str, *, actor: str, ip: str = ""
) -> dict:
    """Print and encode the member's current card on the card printer. Nothing changes in the catalogue or the loans."""
    member = find_member(db, member_no)
    card = _active_card(db, member)
    printer = cast("CardPrinter | None", next((d for d in devices.all() if d.kind == "card_printer"), None))
    if printer is None:
        raise CircError(503, "no_printer", "No card printer is configured")
    try:
        ref = printer.personalise(CardJob(card.uid, member.member_no, member.name, library))
    except (DeviceError, DeviceUnavailable) as exc:
        audit.record(
            db, actor=actor, action="member.card_personalise", entity="member", entity_id=member.member_no,
            outcome="failure", detail={"printer": printer.device_id, "error": str(exc)}, ip=ip,
        )  # fmt: skip
        db.commit()
        raise CircError(503, "printer_failed", f"The card was not printed: {exc}") from exc
    audit.record(
        db, actor=actor, action="member.card_personalise", entity="member", entity_id=member.member_no,
        detail={"printer": printer.device_id, "job": ref}, ip=ip,
    )  # fmt: skip
    db.commit()
    return {"printer": printer.device_id, "job": ref, "member_no": member.member_no, "name": member.name}
