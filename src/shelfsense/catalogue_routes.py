"""HTTP API for the catalogue: locate, create and correct items, and change a copy's status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelfsense.catalogue import service
from shelfsense.deps import Principal, client_ip, get_db, require
from shelfsense.integrations.netcat import NetCatalogueError
from shelfsense.migration import validation
from shelfsense.models import RfidTag
from shelfsense.rfid.events import normalize_tag_uid

router = APIRouter(prefix="/api/catalogue", tags=["catalogue"])


class ItemIn(BaseModel):
    """Fields a librarian can enter. Values are validated by the same rules as the spreadsheet importer."""

    model_config = ConfigDict(extra="forbid")
    title: str = ""  # required, but left to the catalogue validator so the messages match the importer's
    accession_no: str = ""
    author: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    pub_year: int | str | None = None
    edition: str | None = None
    language: str | None = None
    barcode: str | None = None
    call_number: str | None = None
    location: str | None = None
    item_type: str | None = None
    status: str | None = None
    price: float | str | None = None
    acquired_on: str | None = None


def _raise(exc: service.CatalogueError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": exc.issues})


@router.get("/items")
def search_items(
    q: str = "",
    tagged: bool | None = None,
    status: str | None = None,
    limit: int = 25,
    offset: int = 0,
    _: Principal = Depends(require("catalogue.read")),
    db: Session = Depends(get_db),
):
    total, rows, matched = service.search(db, q, tagged=tagged, status=status, limit=limit, offset=offset)
    out = []
    for item, tag in rows:
        entry = service.item_out(item, tag)
        if matched.get(item.id) in ("removed", "retired"):  # found only through a tag that is no longer on it
            entry["matched_tag"] = {"uid": normalize_tag_uid(q), "status": matched[item.id]}
        out.append(entry)
    return {"total": total, "items": out}


@router.get("/lookup")
def lookup_isbn(
    isbn: str,
    request: Request,
    _: Principal = Depends(require("catalogue.write")),
):
    """Net cataloguing: the description of a book from the outside catalogue, to fill in the add-item form."""
    digits = isbn.replace("-", "").replace(" ", "")
    if not (len(digits) == 13 and digits.isdigit()) or not validation.isbn_valid(digits):
        raise HTTPException(
            422,
            detail={"message": "That is not a valid ISBN-13 (13 digits with a correct check digit).", "problems": []},
        )
    try:
        found = request.app.state.netcat.lookup(digits)
    except NetCatalogueError as exc:
        raise HTTPException(502, detail={"message": str(exc) + ". Enter the details by hand.", "problems": []}) from exc
    if found is None:
        raise HTTPException(
            404, detail={"message": "The outside catalogue has no book with that ISBN.", "problems": []}
        )
    return found


@router.get("/items/{item_id}")
def get_item(item_id: int, _: Principal = Depends(require("catalogue.read")), db: Session = Depends(get_db)):
    try:
        item = service.get_item(db, item_id)
    except service.CatalogueError as exc:
        raise _raise(exc) from exc
    tags = db.scalars(select(RfidTag).where(RfidTag.item_id == item_id).order_by(RfidTag.id))
    all_tags = [service.tag_out(t) for t in tags]
    current = next((t for t in all_tags if t and t["status"] == "active"), None)
    out = service.item_out(item)
    out["tag"] = current
    out["tag_history"] = all_tags
    return out


@router.post("/items", status_code=201)
def create_item(
    body: ItemIn,
    request: Request,
    principal: Principal = Depends(require("catalogue.write")),
    db: Session = Depends(get_db),
):
    values = {k: str(v) for k, v in body.model_dump().items() if v is not None}
    try:
        item = service.create_item(db, values, actor=principal.user.username, ip=client_ip(request))
    except service.CatalogueError as exc:
        raise _raise(exc) from exc
    return service.item_out(item)


class ItemPatch(BaseModel):
    """Corrections to a record, or a new status for the copy. Fields left out stay as they are; the accession number
    is fixed (it is written on the tag)."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    pub_year: int | str | None = None
    edition: str | None = None
    language: str | None = None
    barcode: str | None = None
    call_number: str | None = None
    location: str | None = None
    item_type: str | None = None
    status: str | None = None
    price: float | str | None = None
    acquired_on: str | None = None
    reason: str = ""  # why the status changed (e.g. "water damage"), kept in the audit log


@router.patch("/items/{item_id}")
def update_item(
    item_id: int,
    body: ItemPatch,
    request: Request,
    principal: Principal = Depends(require("catalogue.write")),
    db: Session = Depends(get_db),
):
    """Correct a record, or mark a copy damaged, lost, withdrawn or available again. Audited; nothing is deleted."""
    changes = {k: str(v) for k, v in body.model_dump(exclude={"reason"}).items() if v is not None}
    try:
        item = service.update_item(
            db, item_id, changes, reason=body.reason, actor=principal.user.username, ip=client_ip(request)
        )
    except service.CatalogueError as exc:
        raise _raise(exc) from exc
    return service.item_out(item, service.active_tag(db, item.id))
