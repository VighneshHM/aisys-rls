"""Shelves: which shelf locations the library manages, what stands on them, and whether the books are in order.

A shelf is a location (`items.location`). Locations exist as soon as an item is placed there; a managed `Shelf` row adds
a description and a capacity. The virtual bookshelf lays a shelf's items out in call-number order. The order check
compares that order with the order a handheld actually read the books in (an inventory sweep): the books that break an
otherwise ascending run are the ones to move.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aisys import audit
from aisys.models import InventoryScan, InventorySession, Item, RfidTag, Shelf
from aisys.timeutil import iso

HIDDEN_STATUSES = ("withdrawn", "lost")  # not on a shelf any more
DEFAULT_VIEW = 300  # books drawn at once; a location can hold thousands
MAX_VIEW = 1000


class ShelfError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def natural_key(call_number: str) -> tuple:
    """Sort key so 'QA 9' comes before 'QA 10': digit runs compare as numbers, the rest as lower-case text."""
    parts = re.split(r"(\d+)", (call_number or "").strip().lower())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


def shelf_order_check(entries: list[tuple[object, str]]) -> list[object]:
    """Given [(thing, call_number)] in the order they were read, return the things that are out of order.

    The books of the longest run that is already in call-number order stay put; every other book is out of place.
    Books with the same call number never count against each other.
    """
    keys = [natural_key(cn) for _, cn in entries]
    ranking = {k: i for i, k in enumerate(sorted(set(keys)))}
    seq = [ranking[k] for k in keys]
    tails: list[int] = []  # smallest tail of a non-decreasing run of each length
    tail_at: list[int] = []  # index in seq of that tail
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        pos = bisect_right(tails, v)
        if pos == len(tails):
            tails.append(v)
            tail_at.append(i)
        else:
            tails[pos], tail_at[pos] = v, i
        prev[i] = tail_at[pos - 1] if pos else -1
    keep: set[int] = set()
    i = tail_at[-1] if tail_at else -1
    while i != -1:
        keep.add(i)
        i = prev[i]
    return [entries[i][0] for i in range(len(entries)) if i not in keep]


def _clean_code(code: str) -> str:
    code = " ".join((code or "").split())
    if not code or len(code) > 100:
        raise ShelfError(422, "A shelf code is 1 to 100 characters, like 'Main/A1'")
    return code


def _capacity(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ShelfError(422, "Capacity must be a whole number") from None
    if not 0 <= n <= 100_000:
        raise ShelfError(422, "Capacity must be between 0 and 100,000")
    return n


def shelf_out(code: str, shelf: Shelf | None, counts: dict) -> dict:
    cap = shelf.capacity if shelf else None
    return {
        "id": shelf.id if shelf else None,
        "code": code,
        "managed": shelf is not None,
        "description": shelf.description if shelf else "",
        "capacity": cap,
        "items": counts["items"],
        "on_loan": counts["on_loan"],
        "tagged": counts["tagged"],
        "free": (cap - counts["items"]) if cap is not None else None,
        "over_capacity": cap is not None and counts["items"] > cap,
    }


def list_shelves(db: Session) -> list[dict]:
    """Every location that has items, plus every managed shelf (even an empty one)."""
    tagged_ids = select(RfidTag.item_id).where(RfidTag.status == "active")
    rows = db.execute(
        select(
            Item.location,
            func.count(Item.id),
            func.sum(func.iif(Item.status == "on_loan", 1, 0)),
            func.sum(func.iif(Item.id.in_(tagged_ids), 1, 0)),
        )
        .where(Item.location != "", Item.status.not_in(HIDDEN_STATUSES))
        .group_by(Item.location)
    ).all()
    counts = {loc: {"items": n, "on_loan": int(o or 0), "tagged": int(t or 0)} for loc, n, o, t in rows}
    managed = {s.code: s for s in db.scalars(select(Shelf))}
    empty = {"items": 0, "on_loan": 0, "tagged": 0}
    return [shelf_out(code, managed.get(code), counts.get(code, empty)) for code in sorted(set(counts) | set(managed))]


def create_shelf(db: Session, code: str, description: str, capacity, *, actor: str, ip: str = "") -> dict:
    shelf = Shelf(code=_clean_code(code), description=(description or "").strip()[:200], capacity=_capacity(capacity))
    db.add(shelf)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ShelfError(409, f"Shelf '{shelf.code}' is already managed") from None
    audit.record(
        db, actor=actor, action="shelf.create", entity="shelf", entity_id=shelf.id, detail={"code": shelf.code}, ip=ip
    )
    db.commit()
    return next(s for s in list_shelves(db) if s["code"] == shelf.code)


def update_shelf(db: Session, shelf_id: int, fields: dict, *, actor: str, ip: str = "") -> dict:
    shelf = db.get(Shelf, shelf_id)
    if shelf is None:
        raise ShelfError(404, "No such shelf")
    changed: dict[str, Any] = {}
    if "description" in fields and fields["description"] is not None:
        shelf.description = fields["description"].strip()[:200]
        changed["description"] = "changed"
    if "capacity" in fields:
        shelf.capacity = _capacity(fields["capacity"])
        changed["capacity"] = shelf.capacity
    if changed:
        audit.record(
            db,
            actor=actor,
            action="shelf.update",
            entity="shelf",
            entity_id=shelf.id,
            detail={"code": shelf.code, **changed},
            ip=ip,
        )
        db.commit()
    return next(s for s in list_shelves(db) if s["code"] == shelf.code)


def delete_shelf(db: Session, shelf_id: int, *, actor: str, ip: str = "") -> None:
    """Stop managing a shelf. The items on it are not touched (a location exists while it has items)."""
    shelf = db.get(Shelf, shelf_id)
    if shelf is None:
        raise ShelfError(404, "No such shelf")
    audit.record(
        db, actor=actor, action="shelf.delete", entity="shelf", entity_id=shelf.id, detail={"code": shelf.code}, ip=ip
    )
    db.delete(shelf)
    db.commit()


def last_sweep(db: Session, code: str) -> InventorySession | None:
    return db.scalar(
        select(InventorySession)
        .where(InventorySession.location == code, InventorySession.status == "closed")
        .order_by(InventorySession.id.desc())
    )


def out_of_order_items(db: Session, session_id: int, code: str) -> list[int]:
    """Item ids that a sweep read out of call-number order on this shelf (found and shelved-on-loan books only)."""
    rows = db.execute(
        select(InventoryScan.item_id, Item.call_number)
        .join(Item, Item.id == InventoryScan.item_id)
        .where(
            InventoryScan.session_id == session_id,
            InventoryScan.result.in_(("found", "shelved_on_loan")),
            Item.location == code,
        )
        .order_by(InventoryScan.id)
    ).all()
    return shelf_order_check([(item_id, cn) for item_id, cn in rows])  # type: ignore[return-value]


def view(db: Session, code: str, limit: int = DEFAULT_VIEW, offset: int = 0) -> dict:
    """The shelf as a row of books in call-number order, with what the last inventory sweep found out of place."""
    code = _clean_code(code)
    items = list(db.scalars(select(Item).where(Item.location == code, Item.status.not_in(HIDDEN_STATUSES))))
    tagged = set(
        db.scalars(
            select(RfidTag.item_id).where(RfidTag.status == "active", RfidTag.item_id.in_([i.id for i in items] or [0]))
        )
    )
    sweep = last_sweep(db, code)
    misplaced = set(out_of_order_items(db, sweep.id, code)) if sweep else set()
    items.sort(key=lambda i: (natural_key(i.call_number), i.accession_no))
    total = len(items)
    limit, offset = min(max(limit, 1), MAX_VIEW), max(offset, 0)
    items = items[offset : offset + limit]
    managed = db.scalar(select(Shelf).where(Shelf.code == code))
    return {
        "shelf": next(
            (s for s in list_shelves(db) if s["code"] == code),
            shelf_out(code, managed, {"items": 0, "on_loan": 0, "tagged": 0}),
        ),
        "total": total,
        "offset": offset,
        "books": [
            {
                "id": i.id,
                "accession_no": i.accession_no,
                "title": i.bib.title,
                "author": i.bib.author,
                "call_number": i.call_number,
                "status": i.status,
                "tagged": i.id in tagged,
                "is_reference": i.is_reference,
                "out_of_order": i.id in misplaced,
            }
            for i in items
        ],
        "last_sweep": {"session_id": sweep.id, "closed_at": iso(sweep.closed_at), "out_of_order": len(misplaced)}
        if sweep
        else None,
    }
