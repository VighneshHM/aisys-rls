"""HTTP API for item labels: the data for a printable sheet, and a print job for a label printer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.deps import Principal, client_ip, get_db, require
from shelfsense.labels import code128, service
from shelfsense.notifications import queue
from shelfsense.notifications.providers import address_hint, valid_address

router = APIRouter(prefix="/api/labels", tags=["labels"])
CanLabel = Depends(require("catalogue.write"))


def _http(exc: service.LabelError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


def _ids(text: str) -> list[int] | None:
    if not text.strip():
        return None
    try:
        return [int(x) for x in text.split(",") if x.strip()][: service.MAX_LABELS + 1]
    except ValueError:
        raise HTTPException(
            422, detail={"message": "Item numbers are whole numbers separated by commas", "problems": []}
        ) from None


@router.get("/items")
def labels(ids: str = "", location: str = "", _: Principal = CanLabel, db: Session = Depends(get_db)):
    try:
        items = service.pick_items(db, _ids(ids), location.strip() or None)
    except service.LabelError as exc:
        raise _http(exc) from exc
    return {"labels": [service.label_for(i) for i in items], "barcode_max": code128.MAX_LENGTH}


class PrintIn(BaseModel):
    kind: str = Field(pattern="^(spine|barcode)$")
    printer: str = Field(max_length=60)
    ids: list[int] = Field(default_factory=list, max_length=service.MAX_LABELS)
    location: str = Field(default="", max_length=100)


@router.get("/jobs")
def jobs(_: Principal = CanLabel, db: Session = Depends(get_db)):
    """The latest label print jobs and whether each reached its printer."""
    return {"jobs": service.recent_jobs(db)}


@router.post("/print")
def print_job(body: PrintIn, request: Request, principal: Principal = CanLabel, db: Session = Depends(get_db)):
    """Queue the labels as a print job for a label printer (delivered like any queued message)."""
    if not valid_address("print", body.printer):
        raise HTTPException(
            422, detail={"message": f"That is not a usable printer name; use {address_hint('print')}", "problems": []}
        )
    try:
        items = service.pick_items(db, body.ids or None, body.location.strip() or None)
    except service.LabelError as exc:
        raise _http(exc) from exc
    if not items:
        raise HTTPException(422, detail={"message": "No items to print labels for", "problems": []})
    subject, text = service.text_job([service.label_for(i) for i in items], body.kind)
    row = queue.enqueue(db, channel="print", recipient=body.printer, subject=subject, body=text)
    audit.record(
        db, actor=principal.user.username, action="labels.print", entity="printer", entity_id=body.printer,
        detail={"kind": body.kind, "labels": len(items)}, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return {"queued": len(items), "notification_id": row.id, "printer": body.printer}
