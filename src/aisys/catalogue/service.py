"""Catalogue: create and locate items (a bibliographic record plus a physical copy).

Manual entry uses the SAME validator as the spreadsheet importer (migration.validation), so data keyed in by hand and
data migrated from a file obey identical rules.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, contains_eager

from aisys import audit
from aisys.catalogue import fts
from aisys.migration import validation
from aisys.models import BibRecord, Item, RfidTag
from aisys.rfid.events import is_valid_tag_uid, normalize_tag_uid
from aisys.timeutil import iso

ITEM_FIELDS = (
    "title",
    "author",
    "isbn",
    "publisher",
    "pub_year",
    "edition",
    "language",
    "accession_no",
    "barcode",
    "call_number",
    "location",
    "item_type",
    "status",
    "price",
    "acquired_on",
)


class CatalogueError(Exception):
    def __init__(self, status: int, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.issues = issues or []


def tag_out(tag: RfidTag | None) -> dict | None:
    if tag is None:
        return None
    return {
        "uid": tag.tag_uid,
        "status": tag.status,
        "security_armed": tag.security_armed,
        "reader_id": tag.reader_id,
        "tagged_by": tag.tagged_by,
        "tagged_at": iso(tag.tagged_at),
        "ended_at": iso(tag.ended_at),
        "end_reason": tag.end_reason,
    }


def item_out(item: Item, tag: RfidTag | None = None) -> dict:
    b = item.bib
    return {
        "id": item.id,
        "accession_no": item.accession_no,
        "barcode": item.barcode,
        "call_number": item.call_number,
        "location": item.location,
        "item_type": item.item_type,
        "status": item.status,
        "is_reference": item.is_reference,
        "price": str(item.price) if item.price is not None else None,
        "acquired_on": item.acquired_on.isoformat() if item.acquired_on else None,
        "bib_id": b.id,
        "title": b.title,
        "author": b.author,
        "isbn": b.isbn,
        "publisher": b.publisher,
        "pub_year": b.pub_year,
        "edition": b.edition,
        "language": b.language,
        "tag": tag_out(tag),
    }


def active_tag(db: Session, item_id: int) -> RfidTag | None:
    return db.scalar(select(RfidTag).where(RfidTag.item_id == item_id, RfidTag.status == "active"))


def get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise CatalogueError(404, "Item not found")
    return item


def create_item(db: Session, values: dict[str, str], *, actor: str, ip: str = "", commit: bool = True) -> Item:
    """Validate, then insert a bibliographic record (reused if the same title/ISBN exists) and one copy.

    With `commit=False` the rows are flushed but not committed, so a caller can save a whole batch or none of it.
    """
    res = validation.validate_row({k: str(v) for k, v in values.items() if k in ITEM_FIELDS and v is not None})
    if not res.ok:
        raise CatalogueError(
            422, "The item has problems; nothing was saved", [i.as_dict() for i in res.issues if i.severity == "error"]
        )
    c = res.clean

    clash = db.scalar(select(Item).where(func.lower(Item.accession_no) == c["accession_no"].lower()))
    if clash is None and c["barcode"]:
        clash = db.scalar(select(Item).where(func.lower(Item.barcode) == c["barcode"].lower()))
    if clash is not None:
        field = "accession_no" if clash.accession_no.lower() == c["accession_no"].lower() else "barcode"
        raise CatalogueError(
            409,
            f"An item with this {'accession number' if field == 'accession_no' else 'barcode'} already exists",
            [
                {
                    "field": field,
                    "code": "duplicate",
                    "severity": "error",
                    "message": f"Already used by item #{clash.id}",
                }
            ],
        )

    bib = db.scalar(select(BibRecord).where(BibRecord.dedupe_key == c["dedupe_key"]))
    if bib is None:
        bib = BibRecord(
            dedupe_key=c["dedupe_key"],
            title=c["title"],
            author=c["author"],
            isbn=c["isbn"],
            publisher=c["publisher"],
            pub_year=c["pub_year"],
            edition=c["edition"],
            language=c["language"],
        )
        db.add(bib)
        db.flush()
    item = Item(
        bib_id=bib.id,
        accession_no=c["accession_no"],
        barcode=c["barcode"],
        call_number=c["call_number"],
        location=c["location"],
        item_type=c["item_type"],
        status=c["status"],
        is_reference=c["is_reference"],
        price=Decimal(c["price"]) if c["price"] else None,
        acquired_on=date.fromisoformat(c["acquired_on"]) if c["acquired_on"] else None,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:  # lost a race with another request
        db.rollback()
        raise CatalogueError(409, "An item with this accession number or barcode already exists") from exc
    audit.record(
        db,
        actor=actor,
        action="catalogue.item.create",
        entity="item",
        entity_id=item.id,
        detail={"accession_no": item.accession_no, "bib_id": bib.id, "title": bib.title},
        ip=ip,
    )
    if commit:
        db.commit()
    else:  # the caller saves several things together (receiving an order) and commits once
        db.flush()
    db.refresh(item)
    return item


BIB_FIELDS = ("title", "author", "isbn", "publisher", "pub_year", "edition", "language")
COPY_FIELDS = ("barcode", "call_number", "location", "item_type", "status", "price", "acquired_on")
STAFF_STATUSES = ("available", "damaged", "lost", "withdrawn")  # what staff may set; "on_loan" belongs to circulation


def _current(item: Item) -> dict[str, str]:
    out = item_out(item)
    return {k: "" if out[k] is None else str(out[k]) for k in ITEM_FIELDS}


def update_item(
    db: Session, item_id: int, changes: dict[str, str], *, reason: str = "", actor: str, ip: str = ""
) -> Item:
    """Correct a catalogue record or change a copy's status (damaged, lost, withdrawn, back to available).

    The merged record is checked by the same validator as a new item. The accession number is the copy's identity (it is
    written on its tag) and cannot be changed. Title, author, ISBN and the other bibliographic fields belong to the
    record that every copy of the title shares, so a correction there applies to all its copies. Nothing is ever
    deleted: a copy that leaves the collection is marked withdrawn. Every change is audited with the old and new values.
    """
    item = get_item(db, item_id)
    unknown = sorted(set(changes) - set(BIB_FIELDS) - set(COPY_FIELDS))
    if unknown:
        raise CatalogueError(422, f"These fields cannot be changed here: {', '.join(unknown)}")
    before = _current(item)
    wanted = {k: str(v).strip() for k, v in changes.items()}
    if "status" in wanted:
        if wanted["status"] not in STAFF_STATUSES:
            raise CatalogueError(422, f"Status must be one of: {', '.join(STAFF_STATUSES)}")
        if item.status == "on_loan" and wanted["status"] != "on_loan":
            raise CatalogueError(409, "The item is on loan: check it in first, then change its status")
    merged = {**before, **wanted}
    if merged["status"] == "on_loan":  # loans are circulation's business; validate the rest of the record as usual
        merged["status"] = "available"
    res = validation.validate_row(merged)
    # The importer only warns about a bad year or an unknown item type and stores a fallback; a person correcting a
    # record must not lose a value silently, so a warning on a field they changed is refused here (D-063).
    problems = [i.as_dict() for i in res.issues if i.severity == "error" or (i.field in wanted and i.field != "status")]
    if problems:
        for p in problems:
            p["severity"] = "error"
        raise CatalogueError(422, "The item has problems; nothing was saved", problems)
    c = res.clean
    if c["barcode"] and (c["barcode"] or "").lower() != (item.barcode or "").lower():
        clash = db.scalar(select(Item).where(func.lower(Item.barcode) == c["barcode"].lower(), Item.id != item.id))
        if clash is not None:
            raise CatalogueError(
                409,
                "An item with this barcode already exists",
                [
                    {
                        "field": "barcode",
                        "code": "duplicate",
                        "severity": "error",
                        "message": f"Already used by item #{clash.id}",
                    }
                ],
            )
    bib = item.bib
    if c["dedupe_key"] != bib.dedupe_key:
        other = db.scalar(select(BibRecord).where(BibRecord.dedupe_key == c["dedupe_key"], BibRecord.id != bib.id))
        if other is not None:
            raise CatalogueError(
                409,
                f"Another record (#{other.id}) already has this title and ISBN; correct that one instead",
                [
                    {
                        "field": "title",
                        "code": "duplicate",
                        "severity": "error",
                        "message": f"Same as record #{other.id}",
                    }
                ],
            )
        bib.dedupe_key = c["dedupe_key"]
    for k in BIB_FIELDS:
        setattr(bib, k, c[k])
    item.barcode = c["barcode"]
    item.call_number, item.location = c["call_number"], c["location"]
    item.item_type, item.is_reference = c["item_type"], c["is_reference"]
    if item.status != "on_loan":
        item.status = c["status"]
    item.price = Decimal(c["price"]) if c["price"] else None
    item.acquired_on = date.fromisoformat(c["acquired_on"]) if c["acquired_on"] else None
    try:
        db.flush()
    except IntegrityError as exc:  # lost a race with another request
        db.rollback()
        raise CatalogueError(409, "Another item already has that barcode") from exc
    after = _current(item)
    changed = {k: {"from": before[k], "to": after[k]} for k in ITEM_FIELDS if before[k] != after[k]}
    if changed:
        detail: dict = {"accession_no": item.accession_no, "changes": changed}
        if reason.strip():
            detail["reason"] = reason.strip()[:200]
        if any(k in BIB_FIELDS for k in changed):
            detail["copies_sharing_the_record"] = db.scalar(select(func.count()).where(Item.bib_id == bib.id))
        audit.record(
            db, actor=actor, action="catalogue.item.update", entity="item", entity_id=item.id, detail=detail, ip=ip
        )
        db.commit()
    else:
        db.rollback()
    db.refresh(item)
    return item


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(
    db: Session,
    q: str = "",
    *,
    tagged: bool | None = None,
    status: str | None = None,
    hide_statuses: tuple[str, ...] = (),
    limit: int = 25,
    offset: int = 0,
) -> tuple[int, list[tuple[Item, RfidTag | None]], dict[int, str]]:
    """Locate items by title, author, ISBN, publisher, accession no., barcode, call number or tag UID.

    Full-text first (every typed word must match, the last may be unfinished, best match first); the older
    "contains" matching is kept alongside it so a fragment in the middle of a word still finds its item.

    A tag UID matches the item it is on now AND any item it was on before (removed or retired tags too), so a stray
    tag can be traced back. The third result maps item id -> that tag's status for items matched only through it.
    """
    any_link = aliased(RfidTag)
    uid = normalize_tag_uid(q)
    uid = uid if is_valid_tag_uid(uid) else ""
    stmt = (
        select(Item, RfidTag)
        .join(BibRecord, BibRecord.id == Item.bib_id)
        .outerjoin(RfidTag, and_(RfidTag.item_id == Item.id, RfidTag.status == "active"))
        .options(contains_eager(Item.bib))
    )
    q = q.strip()
    ranked = None
    match = fts.match_query(q) if q and fts.available(db) else ""
    indexed = bool(match) and fts.has_hits(db, match)
    if indexed:  # the index answers in milliseconds; scanning every row for "contains" is only for what it cannot find
        ranked = fts.ranked_subquery(match)
        stmt = stmt.outerjoin(ranked, ranked.c.id == Item.id)
        stmt = stmt.where(
            or_(
                ranked.c.id.is_not(None),
                exists().where(any_link.item_id == Item.id, any_link.tag_uid == uid) if uid else false(),
            )
        )
    elif q:
        like = _like(q.lower())
        stmt = stmt.where(
            or_(
                func.lower(BibRecord.title).like(like, escape="\\"),
                func.lower(BibRecord.author).like(like, escape="\\"),
                func.lower(Item.accession_no).like(like, escape="\\"),
                func.lower(func.coalesce(Item.barcode, "")).like(like, escape="\\"),
                func.coalesce(BibRecord.isbn, "").like(_like(q.replace("-", "")), escape="\\"),
                exists().where(any_link.item_id == Item.id, any_link.tag_uid == uid) if uid else false(),
            )
        )
    if tagged is True:
        stmt = stmt.where(RfidTag.id.is_not(None))
    elif tagged is False:
        stmt = stmt.where(RfidTag.id.is_(None))
    if status:
        stmt = stmt.where(Item.status == status)
    if hide_statuses:
        stmt = stmt.where(Item.status.not_in(hide_statuses))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    order = (
        [func.coalesce(ranked.c.rank, 0.0), BibRecord.title, Item.accession_no]
        if ranked is not None
        else [BibRecord.title, Item.accession_no]
    )
    rows = db.execute(stmt.order_by(*order).limit(min(max(limit, 1), 200)).offset(max(offset, 0)))
    found: list[tuple[Item, RfidTag | None]] = [(item, tag) for item, tag in rows.unique()]
    matched: dict[int, str] = {}
    if uid and found:
        links = db.execute(
            select(RfidTag.item_id, RfidTag.status)
            .where(RfidTag.tag_uid == uid, RfidTag.item_id.in_([item.id for item, _ in found]))
            .order_by(RfidTag.id)
        )
        matched = {item_id: status for item_id, status in links}  # the tag's latest status on each item
    return total, found, matched
