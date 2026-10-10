"""HTTP API for acquisitions (purchase orders) and serials (subscriptions).

Needs `acquisitions.manage` and `serials.manage` respectively.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aisys.acquisitions import service as acq
from aisys.deps import Principal, client_ip, get_db, require
from aisys.serials import service as ser

router = APIRouter(prefix="/api/acquisitions", tags=["acquisitions"])
serials = APIRouter(prefix="/api/serials", tags=["serials"])
CanOrder = Depends(require("acquisitions.manage"))
CanSerials = Depends(require("serials.manage"))


def _acq(exc: acq.AcquisitionError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": exc.issues})


def _ser(exc: ser.SerialError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


class LineIn(BaseModel):
    title: str = Field(max_length=500)
    author: str = Field(default="", max_length=300)
    isbn: str | None = Field(default=None, max_length=30)
    publisher: str = Field(default="", max_length=300)
    pub_year: int | None = None
    quantity: int = Field(default=1, strict=True)  # true or "2" is not a quantity (D-080)
    unit_price_cents: int = Field(default=0, strict=True)
    location: str = Field(default="", max_length=100)


class OrderIn(BaseModel):
    supplier: str = Field(max_length=200)
    note: str = Field(default="", max_length=300)
    lines: list[LineIn] = Field(default_factory=list, max_length=acq.MAX_LINES)


class ReceiptIn(BaseModel):
    line_id: int = Field(strict=True)
    quantity: int = Field(strict=True)


class ReceiveIn(BaseModel):
    receipts: list[ReceiptIn] = Field(max_length=acq.MAX_LINES)


# ---------------------------------------------------------------- purchase orders
@router.get("")
def list_orders(
    status: str | None = None, limit: int = 50, offset: int = 0, _: Principal = CanOrder, db: Session = Depends(get_db)
):
    return acq.list_orders(db, status, limit, offset)


@router.post("", status_code=201)
def create_order(body: OrderIn, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)):
    try:
        order = acq.create_order(
            db,
            body.supplier,
            body.note,
            [line.model_dump() for line in body.lines],
            actor=principal.user.username,
            ip=client_ip(request),
        )
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc
    return acq.order_out(order)


@router.get("/{order_id}")
def get_order(order_id: int, _: Principal = CanOrder, db: Session = Depends(get_db)):
    try:
        return acq.order_out(acq.get_order(db, order_id))
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


@router.post("/{order_id}/lines", status_code=201)
def add_line(
    order_id: int, body: LineIn, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)
):
    try:
        return acq.order_out(
            acq.add_line(db, order_id, body.model_dump(), actor=principal.user.username, ip=client_ip(request))
        )
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


@router.delete("/{order_id}/lines/{line_id}")
def remove_line(
    order_id: int, line_id: int, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)
):
    try:
        return acq.order_out(
            acq.remove_line(db, order_id, line_id, actor=principal.user.username, ip=client_ip(request))
        )
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


@router.post("/{order_id}/place")
def place_order(order_id: int, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)):
    try:
        return acq.order_out(acq.place(db, order_id, actor=principal.user.username, ip=client_ip(request)))
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


@router.post("/{order_id}/cancel")
def cancel_order(order_id: int, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)):
    try:
        return acq.order_out(acq.cancel(db, order_id, actor=principal.user.username, ip=client_ip(request)))
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


@router.post("/{order_id}/receive")
def receive(
    order_id: int, body: ReceiveIn, request: Request, principal: Principal = CanOrder, db: Session = Depends(get_db)
):
    try:
        return acq.receive(
            db, order_id, [r.model_dump() for r in body.receipts], actor=principal.user.username, ip=client_ip(request)
        )
    except acq.AcquisitionError as exc:
        raise _acq(exc) from exc


# ---------------------------------------------------------------- serials
class SerialIn(BaseModel):
    title: str = Field(max_length=500)
    issn: str = Field(default="", max_length=9)
    frequency: str = Field(max_length=12)
    supplier: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=100)
    first_expected_on: date
    issues_ahead: int = Field(default=6, strict=True)


class ExtendIn(BaseModel):
    count: int = Field(default=6, strict=True)


class ActiveIn(BaseModel):
    active: bool


class ReceiveIssueIn(BaseModel):
    received_on: date | None = None
    book_in: bool = False


class MarkIn(BaseModel):
    status: str = Field(max_length=10)
    note: str = Field(default="", max_length=200)


@serials.get("")
def list_serials(_: Principal = CanSerials, db: Session = Depends(get_db)):
    return {"serials": ser.list_titles(db)}


@serials.get("/late")
def late(_: Principal = CanSerials, db: Session = Depends(get_db)):
    return {"late": ser.late_issues(db)}


@serials.post("", status_code=201)
def create_serial(body: SerialIn, request: Request, principal: Principal = CanSerials, db: Session = Depends(get_db)):
    try:
        s = ser.create_title(db, **body.model_dump(), actor=principal.user.username, ip=client_ip(request))
    except ser.SerialError as exc:
        raise _ser(exc) from exc
    return ser.title_out(s, with_issues=True)


@serials.get("/{serial_id}")
def get_serial(serial_id: int, _: Principal = CanSerials, db: Session = Depends(get_db)):
    try:
        return ser.title_out(ser.get_title(db, serial_id), with_issues=True)
    except ser.SerialError as exc:
        raise _ser(exc) from exc


@serials.post("/{serial_id}/extend")
def extend_serial(
    serial_id: int, body: ExtendIn, request: Request, principal: Principal = CanSerials, db: Session = Depends(get_db)
):
    try:
        return ser.title_out(
            ser.extend(db, serial_id, body.count, actor=principal.user.username, ip=client_ip(request)),
            with_issues=True,
        )
    except ser.SerialError as exc:
        raise _ser(exc) from exc


@serials.post("/{serial_id}/active")
def set_active(
    serial_id: int, body: ActiveIn, request: Request, principal: Principal = CanSerials, db: Session = Depends(get_db)
):
    try:
        return ser.title_out(
            ser.set_active(db, serial_id, body.active, actor=principal.user.username, ip=client_ip(request))
        )
    except ser.SerialError as exc:
        raise _ser(exc) from exc


@serials.post("/issues/{issue_id}/receive")
def receive_issue(
    issue_id: int,
    body: ReceiveIssueIn,
    request: Request,
    principal: Principal = CanSerials,
    db: Session = Depends(get_db),
):
    try:
        return ser.receive_issue(
            db,
            issue_id,
            received_on=body.received_on,
            book_in=body.book_in,
            actor=principal.user.username,
            ip=client_ip(request),
        )
    except ser.SerialError as exc:
        raise _ser(exc) from exc


@serials.post("/issues/{issue_id}/mark")
def mark_issue(
    issue_id: int, body: MarkIn, request: Request, principal: Principal = CanSerials, db: Session = Depends(get_db)
):
    try:
        return ser.mark(db, issue_id, body.status, body.note, actor=principal.user.username, ip=client_ip(request))
    except ser.SerialError as exc:
        raise _ser(exc) from exc
