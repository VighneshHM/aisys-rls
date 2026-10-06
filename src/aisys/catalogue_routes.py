"""HTTP API for the catalogue: locate and create items."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys.catalogue import service
from aisys.deps import Principal, client_ip, get_db, require
from aisys.models import RfidTag
from aisys.rfid.events import normalize_tag_uid

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
