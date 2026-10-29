"""Tagging workflow (FR05): validate -> encode on the device -> bind in the database, with rollback on failure.

Order matters. We never save an association the device did not accept, and if the database save fails after the device
accepted the write, the device write is undone (`clear`). A real reader cannot join a database transaction, so this
compensating step is the honest equivalent.

Tag lifecycle (one `rfid_tags` row per link between a tag and an item):
    unknown --link--> active --remove--> removed --link--> active ...
                         \\--retire--> retired (permanent)      removed --retire--> retired
A removed tag can be linked again to any item; a retired tag (damaged or lost) can never be linked again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import case, exists, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, contains_eager

from shelfsense import audit
from shelfsense.catalogue.service import active_tag, item_out, tag_out
from shelfsense.models import BibRecord, Card, Item, Member, RfidTag, TagEvent
from shelfsense.rfid.devices import DeviceError, DeviceUnavailable, StaffReader
from shelfsense.rfid.events import TagObserved, is_valid_tag_uid, normalize_tag_uid
from shelfsense.rfid.registry import DeviceRegistry
from shelfsense.timeutil import iso, utcnow

log = logging.getLogger("shelfsense.rfid")
BLOCKED_ITEM_STATUSES = {"lost", "withdrawn"}
ENDINGS = ("removed", "retired")  # how a link can end


class TaggingError(Exception):
    def __init__(self, status: int, message: str, problems: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.problems = problems or []


@dataclass
class Check:
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    item: Item | None = None
    current_tag: RfidTag | None = None  # the item's active tag, if any
    uid: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def status_code(self) -> int:
        codes = {p["code"] for p in self.problems}
        if "item_not_found" in codes:
            return 404
        return 422 if codes & {"tag_uid_invalid"} else 409

    def out(self) -> dict:
        return {
            "ok": self.ok,
            "tag_uid": self.uid,
            "problems": self.problems,
            "warnings": self.warnings,
            "item": item_out(self.item, self.current_tag) if self.item else None,
        }


def _p(code: str, message: str, field_: str = "") -> dict:
    return {"code": code, "field": field_, "message": message}


# ------------------------------------------------------------------ tag state
def tag_links(db: Session, uid: str) -> list[RfidTag]:
    """Every link this tag has ever had, oldest first."""
    return list(db.scalars(select(RfidTag).where(RfidTag.tag_uid == uid).order_by(RfidTag.id)))


def state_of(links: list[RfidTag]) -> str:
    """unknown (never linked) | active | removed (reusable) | retired (permanent)."""
    if not links:
        return "unknown"
    statuses = {link.status for link in links}
    return "retired" if "retired" in statuses else "active" if "active" in statuses else "removed"


# ------------------------------------------------------------------ validation (dry run)
def validate_tagging(
    db: Session, item_id: int, tag_uid: str, replace: bool = False, old_tag_fate: str = "removed"
) -> Check:
    uid = normalize_tag_uid(tag_uid)
    chk = Check(uid=uid)

    item = db.get(Item, item_id)
    if item is None:
        chk.problems.append(_p("item_not_found", f"No item with id {item_id}", "item_id"))
        return chk
    chk.item = item
    chk.current_tag = active_tag(db, item.id)

    if item.status in BLOCKED_ITEM_STATUSES:
        chk.problems.append(_p("item_status", f"Item is {item.status} and cannot be tagged", "item_id"))
    if not item.bib.title.strip():
        chk.problems.append(_p("title_missing", "The bibliographic record has no title", "title"))
    if not item.accession_no.strip():
        chk.problems.append(_p("accession_missing", "The item has no accession number to write on the tag", "item_id"))
    if old_tag_fate not in ENDINGS:
        chk.problems.append(_p("bad_fate", "The old tag must be either 'removed' or 'retired'", "old_tag_fate"))

    if not is_valid_tag_uid(uid):
        chk.problems.append(_p("tag_uid_invalid", "Tag UID must be 8-32 hexadecimal characters", "tag_uid"))
        return chk

    links = tag_links(db, uid)
    state = state_of(links)
    if state == "active":
        on = next(link for link in links if link.status == "active")
        if on.item_id == item.id:
            chk.problems.append(_p("tag_already_on_item", "This tag is already associated with this item", "tag_uid"))
        else:
            chk.problems.append(
                _p(
                    "tag_bound_elsewhere",
                    f"This tag is already on item #{on.item_id} ({on.item.accession_no})",
                    "tag_uid",
                )
            )
    elif state == "retired":
        chk.problems.append(
            _p(
                "tag_retired",
                "This tag was retired (taken out of service) and cannot be used again; use a new tag",
                "tag_uid",
            )
        )
    elif state == "removed":
        last = links[-1]
        chk.warnings.append(
            _p(
                "tag_reused",
                f"This tag was used before (on {last.item.accession_no}) and removed; it will be linked again",
                "tag_uid",
            )
        )

    if chk.current_tag is not None and chk.current_tag.tag_uid != uid:
        if replace:
            chk.warnings.append(
                _p(
                    "will_replace_old_tag",
                    f"The item's current tag {chk.current_tag.tag_uid} will be "
                    + ("retired (never used again)" if old_tag_fate == "retired" else "removed (it can be reused)"),
                    "replace",
                )
            )
        else:
            chk.problems.append(
                _p(
                    "item_already_tagged",
                    f"Item already has tag {chk.current_tag.tag_uid}; confirm re-tagging to replace it",
                    "replace",
                )
            )
    return chk


# ------------------------------------------------------------------ member side (patron cards)
CARD_RE = re.compile(r"^[A-Z0-9]{6,64}$")


@dataclass
class MemberCheck:
    """The member-side twin of `Check`: is this member record fit to carry this card (a new card or a re-tag)?"""

    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    member: Member | None = None
    current_card: Card | None = None  # the member's active patron card, if any
    uid: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def status_code(self) -> int:
        codes = {p["code"] for p in self.problems}
        if "no_such_member" in codes:
            return 404
        return 409 if codes <= {"card_in_use", "member_removed"} else 422

    def out(self) -> dict:
        m = self.member
        return {
            "ok": self.ok,
            "card_uid": self.uid,
            "problems": self.problems,
            "warnings": self.warnings,
            "member": {"member_no": m.member_no, "name": m.name, "status": m.status} if m else None,
            "current_card": self.current_card.uid if self.current_card else None,
        }


def normalize_card_uid(uid: str) -> str:
    return re.sub(r"[\s:-]", "", uid or "").upper()


def card_problems(db: Session, uid: str) -> list[dict]:
    """Checks on the card number alone, shared by a new member's first card and any later one."""
    if not CARD_RE.match(uid):
        return [_p("bad_card", "A card number is 6 to 64 letters and digits", "card_uid")]
    if db.scalar(select(Card).where(Card.uid == uid)):
        return [_p("card_in_use", "That card number is already registered to someone", "card_uid")]
    return []


def validate_member_tagging(db: Session, member_no: str, card_uid: str) -> MemberCheck:
    """Validate the member record before a card is linked to it (FR05). Every problem at once, nothing written.

    Used by the circulation desk and by the Administration page, so both paths apply the same rules.
    """
    uid = normalize_card_uid(card_uid)
    chk = MemberCheck(uid=uid)
    member = db.scalar(select(Member).where(Member.member_no == (member_no or "").strip().upper()))
    if member is None:
        chk.problems.append(_p("no_such_member", f"No member with number '{member_no}'", "member_no"))
        return chk
    chk.member = member
    if member.removed_at is not None:
        chk.problems.append(
            _p("member_removed", f"{member.name} has been removed: restore them before issuing a card", "member_no")
        )
    if not member.name.strip():
        chk.problems.append(_p("name_missing", "The member record has no name to print on the card", "name"))
    chk.problems += card_problems(db, uid)
    chk.current_card = db.scalar(
        select(Card).where(Card.kind == "patron", Card.member_ref == member.member_no, Card.active)
    )
    if chk.current_card is not None:
        chk.warnings.append(
            _p(
                "will_replace_old_card",
                f"The member's card {chk.current_card.uid} will be switched off (lost or replaced)",
                "card_uid",
            )
        )
    if member.status == "blocked":
        chk.warnings.append(
            _p("member_blocked", "The member is blocked: the card will identify them, but loans stay refused", "")
        )
    return chk


# ------------------------------------------------------------------ device access
def get_reader(registry: DeviceRegistry, reader_id: str) -> StaffReader:
    reader = registry.get(reader_id, "staff_reader")
    if reader is None:
        raise TaggingError(404, f"Unknown staff reader '{reader_id}'")
    return reader  # type: ignore[return-value]


def _event(db: Session, uid: str, reader_id: str, kind: str, *, item_id: int | None, actor: str, detail: dict) -> None:
    db.add(TagEvent(tag_uid=uid, reader_id=reader_id, event_type=kind, item_id=item_id, actor=actor, detail=detail))


def read_field(db: Session, registry: DeviceRegistry, reader_id: str, *, actor: str) -> list[dict]:
    """What is on the reader right now, enriched with what we know about each tag. Each sighting is recorded."""
    reader = get_reader(registry, reader_id)
    try:
        seen: list[TagObserved] = reader.read_field()
    except DeviceUnavailable as exc:
        raise TaggingError(503, f"Reader '{reader_id}' is offline") from exc
    except DeviceError as exc:
        raise TaggingError(502, f"Reader error: {exc}") from exc

    out = []
    for obs in seen:
        links = tag_links(db, obs.uid)
        state = state_of(links)
        last = links[-1] if links else None
        item = last.item if last else None
        out.append(
            {
                "uid": obs.uid,
                "reader_id": obs.reader_id,
                "item_ref_on_tag": obs.item_ref,
                "security_armed": obs.security_armed,
                "state": state,
                "tag_status": None if state == "unknown" else state,
                "known": state != "unknown",
                "usable": state in ("unknown", "removed"),  # active and retired tags cannot be linked
                "blank": obs.item_ref is None and state in ("unknown", "removed"),
                "item": item_out(item, last if state == "active" else None) if item else None,
            }
        )
        _event(
            db,
            obs.uid,
            reader_id,
            "read",
            item_id=item.id if item else None,
            actor=actor,
            detail={"state": state, "item_ref": obs.item_ref},
        )
    db.commit()
    return out


# ------------------------------------------------------------------ associate / re-tag
def associate(
    db: Session,
    registry: DeviceRegistry,
    *,
    item_id: int,
    tag_uid: str,
    reader_id: str,
    replace: bool,
    reason: str,
    actor: str,
    ip: str = "",
    old_tag_fate: str = "removed",
) -> dict:
    reader = get_reader(registry, reader_id)
    chk = validate_tagging(db, item_id, tag_uid, replace, old_tag_fate)
    if not chk.ok:
        _indicate(reader, "error")
        audit.record(
            db,
            actor=actor,
            action="rfid.tag",
            outcome="denied",
            entity="item",
            entity_id=item_id,
            detail={"tag_uid": chk.uid, "problems": [p["code"] for p in chk.problems]},
            ip=ip,
        )
        db.commit()
        raise TaggingError(chk.status_code(), "The item cannot be tagged", chk.problems)
    item, old, uid = chk.item, chk.current_tag, chk.uid
    assert item is not None  # validate_tagging returned ok
    reused = state_of(tag_links(db, uid)) == "removed"

    # 1) device first: nothing is saved that the hardware did not accept
    try:
        reader.encode(uid, item.accession_no)
    except DeviceUnavailable as exc:
        _fail(db, reader, uid, reader_id, item.id, actor, ip, "offline")
        raise TaggingError(503, f"Reader '{reader_id}' is offline; nothing was saved") from exc
    except DeviceError as exc:
        _fail(db, reader, uid, reader_id, item.id, actor, ip, str(exc))
        raise TaggingError(
            409, f"The reader could not write the tag ({exc}); nothing was saved. Is the tag on the reader?"
        ) from exc

    # 2) then the database, all-or-nothing
    now = utcnow()
    try:
        if old is not None:
            old.status, old.ended_at, old.security_armed = old_tag_fate, now, False
            old.end_reason = (reason or f"replaced by a new tag ({old_tag_fate})")[:200]
            _event(
                db,
                old.tag_uid,
                reader_id,
                old_tag_fate,
                item_id=item.id,
                actor=actor,
                detail={"reason": old.end_reason, "replaced_by": uid},
            )
            db.flush()  # free the one-active-tag-per-item slot before inserting the new link
        tag = RfidTag(
            tag_uid=uid,
            item_id=item.id,
            status="active",
            security_armed=True,
            reader_id=reader_id,
            tagged_by=actor,
            tagged_at=now,
        )
        db.add(tag)
        db.flush()
        _event(
            db,
            uid,
            reader_id,
            "retagged" if old is not None else "tagged",
            item_id=item.id,
            actor=actor,
            detail={"accession_no": item.accession_no, "replaced": old.tag_uid if old else None, "reused_tag": reused},
        )
        audit.record(
            db,
            actor=actor,
            action="rfid.retag" if old else "rfid.tag",
            entity="item",
            entity_id=item.id,
            detail={
                "tag_uid": uid,
                "accession_no": item.accession_no,
                "replaced": old.tag_uid if old else None,
                "old_tag_fate": old_tag_fate if old else None,
                "reused_tag": reused,
            },
            ip=ip,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _undo_device(reader, uid)
        _indicate(reader, "error")
        raise TaggingError(409, "The tag or item was changed by someone else; nothing was saved") from exc
    except Exception:
        db.rollback()
        _undo_device(reader, uid)
        _indicate(reader, "error")
        raise

    _indicate(reader, "ok")
    db.refresh(tag)
    return {
        "tag": tag_out(tag),
        "item": item_out(item, tag),
        "previous_tag": tag_out(old) if old else None,
        "reused_tag": reused,
        "indication": "ok",
    }


def _indicate(reader: StaffReader, pattern: str) -> None:
    try:
        reader.indicate(pattern)
    except DeviceError:  # a failed beep must never fail the business operation
        log.warning("could not signal '%s' on reader %s", pattern, reader.device_id)


def _undo_device(reader: StaffReader, uid: str) -> None:
    try:
        reader.clear(uid)
    except DeviceError:
        log.error("tag %s was encoded but the association failed and the tag could not be cleared", uid)


def _fail(
    db: Session, reader: StaffReader, uid: str, reader_id: str, item_id: int, actor: str, ip: str, why: str
) -> None:
    _indicate(reader, "error")
    _event(db, uid, reader_id, "encode_failed", item_id=item_id, actor=actor, detail={"reason": why})
    audit.record(
        db,
        actor=actor,
        action="rfid.tag",
        outcome="failure",
        entity="item",
        entity_id=item_id,
        detail={"tag_uid": uid, "reason": why},
        ip=ip,
    )
    db.commit()


# ------------------------------------------------------------------ remove / retire
def _erase_on_device(registry: DeviceRegistry, reader_id: str, uid: str) -> dict:
    """Best effort: if the tag is on the reader, wipe it and switch its security bit off.

    Ending a tag must never be blocked by hardware: the tag may already be off the book, or the reader may be down.
    The outcome is reported so staff know whether the physical tag still needs attention.
    """
    reader = registry.get(reader_id, "staff_reader")
    if reader is None:
        found = [d for d in registry.all() if d.kind == "staff_reader"]
        reader = found[0] if found else None
    if reader is None:
        return {"status": "no_reader", "message": "No staff reader is configured, so the tag was not erased."}
    try:
        if not any(t.uid == uid for t in reader.read_field()):  # type: ignore[attr-defined]
            return {
                "status": "not_on_reader",
                "reader_id": reader.device_id,
                "message": "The tag was not on the reader, so it could not be erased. Remove or deactivate the "
                "physical tag yourself.",
            }
        reader.clear(uid)  # type: ignore[attr-defined]
    except DeviceUnavailable:
        return {
            "status": "reader_offline",
            "reader_id": reader.device_id,
            "message": "The reader is offline, so the tag could not be erased. Remove or deactivate the "
            "physical tag yourself.",
        }
    except DeviceError as exc:
        return {
            "status": "device_error",
            "reader_id": reader.device_id,
            "message": f"The reader could not erase the tag ({exc}). Remove or deactivate the physical tag yourself.",
        }
    _indicate(reader, "ok")  # type: ignore[arg-type]
    return {
        "status": "cleared",
        "reader_id": reader.device_id,
        "message": "The reader erased the tag and switched its security bit off.",
    }


def end_tag(
    db: Session,
    registry: DeviceRegistry,
    tag_uid: str,
    *,
    fate: str,
    reason: str,
    actor: str,
    ip: str = "",
    reader_id: str | None = None,
) -> dict:
    """End a tag's link to its item as `removed` (reusable) or `retired` (permanent).

    A removed tag can also be retired later (found damaged), without needing to be on an item.
    """
    if fate not in ENDINGS:
        raise TaggingError(422, "fate must be 'removed' or 'retired'")
    uid = normalize_tag_uid(tag_uid)
    links = tag_links(db, uid)
    if not links:
        raise TaggingError(404, "This tag has never been linked to an item, so there is nothing to " + fate[:-1] + ".")
    state = state_of(links)
    if state == "retired":
        raise TaggingError(409, "Tag is already retired")
    on = next((link for link in links if link.status == "active"), None)
    now = utcnow()

    if on is None:  # the tag is currently 'removed'
        if fate == "removed":
            raise TaggingError(409, "This tag is not on an item: it was already removed")
        row = links[-1]
        device = {"status": "not_linked", "message": "The tag was not on an item. It is now out of service for good."}
        earlier = f"{row.end_reason}; " if row.end_reason else ""
        row.status = "retired"
        row.end_reason = f"{earlier}retired: {reason or 'taken out of service'}"[:200]
    else:
        row = on
        device = _erase_on_device(registry, reader_id or row.reader_id or "staff-1", uid)
        row.status, row.ended_at, row.security_armed = fate, now, False
        row.end_reason = (reason or fate)[:200]

    _event(
        db,
        uid,
        row.reader_id,
        fate,
        item_id=row.item_id,
        actor=actor,
        detail={"reason": row.end_reason, "device": device["status"]},
    )
    audit.record(
        db,
        actor=actor,
        action="rfid.remove" if fate == "removed" else "rfid.retire",
        entity="item",
        entity_id=row.item_id,
        detail={"tag_uid": uid, "reason": row.end_reason, "device": device["status"]},
        ip=ip,
    )
    db.commit()
    return {**tag_out(row), "state": fate, "device": device}  # type: ignore[dict-item]


def remove_tag(db: Session, registry: DeviceRegistry, tag_uid: str, **kw) -> dict:
    return end_tag(db, registry, tag_uid, fate="removed", **kw)


def retire_tag(db: Session, registry: DeviceRegistry, tag_uid: str, **kw) -> dict:
    return end_tag(db, registry, tag_uid, fate="retired", **kw)


# ------------------------------------------------------------------ lookup + registry
def tag_detail(db: Session, tag_uid: str, history_limit: int = 50) -> dict:
    """The tag-to-item relationship in both directions, every link the tag has had, and its activity."""
    uid = normalize_tag_uid(tag_uid)
    links = tag_links(db, uid)
    if not links:
        raise TaggingError(404, "No item has ever been associated with this tag")
    state = state_of(links)
    current = next((link for link in links if link.status == "active"), links[-1])
    events = db.scalars(
        select(TagEvent).where(TagEvent.tag_uid == uid).order_by(TagEvent.id.desc()).limit(history_limit)
    )
    siblings = db.scalars(select(RfidTag).where(RfidTag.item_id == current.item_id).order_by(RfidTag.id))
    return {
        "state": state,
        "tag": tag_out(current),
        "item": item_out(current.item, active_tag(db, current.item_id)),
        "associations": [
            {
                **(tag_out(link) or {}),
                "item": {"id": link.item_id, "accession_no": link.item.accession_no, "title": link.item.bib.title},
            }
            for link in links
        ],
        "tags_on_item": [tag_out(t) for t in siblings],
        "history": [event_out(e) for e in events],
    }


def list_tags(db: Session, *, state: str | None = None, q: str = "", limit: int = 25, offset: int = 0) -> dict:
    """The tag registry: one line per physical tag with its state, newest activity first."""
    agg = (
        select(
            RfidTag.tag_uid.label("uid"),
            func.count().label("links"),
            func.max(case((RfidTag.status == "retired", 1), else_=0)).label("any_retired"),
            func.max(case((RfidTag.status == "active", 1), else_=0)).label("any_active"),
            func.max(RfidTag.id).label("latest_id"),
        )
        .group_by(RfidTag.tag_uid)
        .subquery()
    )
    state_col = case((agg.c.any_retired == 1, "retired"), (agg.c.any_active == 1, "active"), else_="removed")

    counts = {"active": 0, "removed": 0, "retired": 0}
    for s, n in db.execute(select(state_col, func.count()).group_by(state_col)):
        counts[s] = n

    stmt = (
        select(RfidTag, agg.c.links, state_col.label("state"))
        .join(agg, agg.c.latest_id == RfidTag.id)
        .join(Item, Item.id == RfidTag.item_id)
        .join(BibRecord, BibRecord.id == Item.bib_id)
        .options(contains_eager(RfidTag.item).contains_eager(Item.bib))
    )
    if state in counts:
        stmt = stmt.where(state_col == state)
    q = q.strip()
    if q:

        def esc(text: str) -> str:
            return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        like, uid_like = esc(q.lower()), esc(normalize_tag_uid(q))
        # match the tag's own UID, or ANY item it has ever been on (so an old item still leads to its tag)
        past, past_item, past_bib = aliased(RfidTag), aliased(Item), aliased(BibRecord)
        on_matching_item = exists().where(
            past.tag_uid == agg.c.uid,
            past_item.id == past.item_id,
            past_bib.id == past_item.bib_id,
            or_(
                func.lower(past_item.accession_no).like(like, escape="\\"),
                func.lower(past_bib.title).like(like, escape="\\"),
            ),
        )
        uid_match = func.lower(agg.c.uid).like(uid_like, escape="\\") if normalize_tag_uid(q) else false()
        stmt = stmt.where(or_(uid_match, on_matching_item))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.execute(stmt.order_by(RfidTag.id.desc()).limit(min(max(limit, 1), 200)).offset(max(offset, 0))).unique()
    return {
        "total": total,
        "counts": counts,
        "tags": [
            {
                "uid": link.tag_uid,
                "state": st,
                "links": n,
                "item": {"id": link.item_id, "accession_no": link.item.accession_no, "title": link.item.bib.title},
                "tagged_at": iso(link.tagged_at),
                "tagged_by": link.tagged_by,
                "ended_at": iso(link.ended_at),
                "end_reason": link.end_reason,
            }
            for link, n, st in rows
        ],
    }


def event_out(e: TagEvent) -> dict:
    return {
        "id": e.id,
        "ts": iso(e.ts),
        "tag_uid": e.tag_uid,
        "reader_id": e.reader_id,
        "type": e.event_type,
        "item_id": e.item_id,
        "actor": e.actor,
        "detail": e.detail,
    }
