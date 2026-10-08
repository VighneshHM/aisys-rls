"""Members: create them, issue or replace their patron card, find them by name.

A patron card only identifies a member; it never gives a login. Issuing a new card to a member who already has one
(lost, damaged) switches the old card off in the same transaction, so a lost card cannot be used any more.
"""

from __future__ import annotations

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.circulation.service import CircError, find_member
from aisys.models import Card, Member
from aisys.notifications.providers import valid_address

CARD_RE = re.compile(r"^[A-Z0-9]{6,64}$")
MEMBER_NO_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")


def next_member_no(db: Session) -> str:
    """M0001, M0002, ...: one more than the highest M-number in use (other formats are left alone)."""
    numbers = [int(m[1:]) for (m,) in db.execute(select(Member.member_no)) if re.fullmatch(r"M\d+", m)]
    return f"M{max(numbers, default=0) + 1:04d}"


def _clean_card(uid: str) -> str:
    uid = re.sub(r"[\s:-]", "", uid or "").upper()
    if not CARD_RE.match(uid):
        raise CircError(422, "bad_card", "A card number is 6 to 64 letters and digits")
    return uid


def _check_card_free(db: Session, uid: str) -> None:
    if db.scalar(select(Card).where(Card.uid == uid)):
        raise CircError(409, "card_in_use", "That card number is already registered to someone")


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
    uid = _clean_card(card_uid) if card_uid.strip() else ""
    if uid:
        _check_card_free(db, uid)
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
        detail={"name": name, "card": bool(uid)},
        ip=ip,
    )
    db.commit()
    return member


def issue_card(db: Session, member_no: str, card_uid: str, *, actor: str, ip: str = "") -> Member:
    """Give a member a card. Any card they had before is switched off (lost or replaced)."""
    member = find_member(db, member_no)
    uid = _clean_card(card_uid)
    _check_card_free(db, uid)
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


def search(db: Session, q: str, limit: int = 10) -> list[dict]:
    """Members whose name or number contains `q` (case-insensitive), best matches first."""
    q = q.strip().lower().replace("%", "").replace("_", "")  # no wildcards: they would match everyone
    if not q:
        return []
    like = f"%{q}%"
    rows = db.scalars(
        select(Member)
        .where(or_(func.lower(Member.name).like(like), func.lower(Member.member_no).like(like)))
        .order_by(Member.name)
        .limit(limit)
    )
    rows = list(rows)
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
            "status": m.status,
            "fines_cents": m.fines_cents,
            "card": cards.get(m.member_no),
        }
        for m in rows
    ]
