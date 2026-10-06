"""HTTP API for circulation: members, check-out / renew / check-in, due-date enquiry, history, and the dev simulator."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys.catalogue.service import active_tag
from aisys.circulation import service
from aisys.circulation.connector import CirculationConfig
from aisys.circulation.mock_ilms import MockIlms
from aisys.circulation.resilience import ResilientConnector
from aisys.config import Settings
from aisys.deps import Principal, client_ip, get_db, get_principal, get_settings, require
from aisys.models import Card, Member
from aisys.rfid.mock import MockStaffReader
from aisys.rfid.registry import DeviceRegistry

router = APIRouter(prefix="/api/circulation", tags=["circulation"])
CIRC_PERMISSIONS = ("circulation.checkout", "circulation.checkin", "circulation.renew")


def desk_staff(principal: Principal = Depends(get_principal)) -> Principal:
    """Anyone who can do at least one circulation action may use the desk's lookups."""
    if not principal.permissions.intersection(CIRC_PERMISSIONS):
        raise HTTPException(403, "Missing permission: circulation.checkout, circulation.checkin or circulation.renew")
    return principal


def history_viewer(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.permissions.intersection({"member.read", "audit.read", *CIRC_PERMISSIONS}):
        raise HTTPException(403, "Missing permission: member.read or audit.read")
    return principal


def get_connector(request: Request) -> ResilientConnector:
    return request.app.state.ilms


def get_devices(request: Request) -> DeviceRegistry:
    return request.app.state.devices


def get_config(settings: Settings = Depends(get_settings)) -> CirculationConfig:
    return settings.circulation


class IdentifyIn(BaseModel):
    uid: str = Field(min_length=1, max_length=64)


class BlockIn(BaseModel):
    blocked: bool
    reason: str = Field(default="", max_length=200)


class CheckoutIn(BaseModel):
    member_no: str = Field(min_length=1, max_length=32)
    item_ref: str = Field(min_length=1, max_length=64)
    reader_id: str = "staff-1"


class ItemActionIn(BaseModel):
    item_ref: str = Field(min_length=1, max_length=64)
    reader_id: str = "staff-1"


def _http(exc: service.CircError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "code": exc.code, "problems": []})


# ------------------------------------------------------------------ members
@router.post("/members/identify")
def identify_member(
    body: IdentifyIn,
    request: Request,
    principal: Principal = Depends(require("member.read")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(get_config),
):
    """A patron card scan: shows the member. It does not log anyone in and changes no session."""
    try:
        member = service.identify(db, body.uid, actor=principal.user.username, ip=client_ip(request))
    except service.CircError as exc:
        raise _http(exc) from exc
    return service.member_out(db, member, config)


@router.get("/members/{member_no}")
def get_member(
    member_no: str,
    _: Principal = Depends(require("member.read")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(get_config),
):
    try:
        return service.member_out(db, service.find_member(db, member_no), config)
    except service.CircError as exc:
        raise _http(exc) from exc


@router.post("/members/{member_no}/block")
def block_member(
    member_no: str,
    body: BlockIn,
    request: Request,
    principal: Principal = Depends(require("member.write")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(get_config),
):
    try:
        member = service.set_blocked(
            db, member_no, body.blocked, body.reason, actor=principal.user.username, ip=client_ip(request)
        )
    except service.CircError as exc:
        raise _http(exc) from exc
    return service.member_out(db, member, config)


# ------------------------------------------------------------------ transactions
@router.post("/checkout")
def checkout(
    body: CheckoutIn,
    principal: Principal = Depends(require("circulation.checkout")),
    db: Session = Depends(get_db),
    connector: ResilientConnector = Depends(get_connector),
    devices: DeviceRegistry = Depends(get_devices),
    config: CirculationConfig = Depends(get_config),
):
    try:
        return service.checkout(
            db,
            connector,
            devices,
            config,
            member_no=body.member_no,
            item_ref=body.item_ref,
            actor=principal.user.username,
            reader_id=body.reader_id,
        )
    except service.CircError as exc:
        raise _http(exc) from exc


@router.post("/renew")
def renew(
    body: ItemActionIn,
    principal: Principal = Depends(require("circulation.renew")),
    db: Session = Depends(get_db),
    connector: ResilientConnector = Depends(get_connector),
    devices: DeviceRegistry = Depends(get_devices),
    config: CirculationConfig = Depends(get_config),
):
    try:
        return service.renew(
            db,
            connector,
            devices,
            config,
            item_ref=body.item_ref,
            actor=principal.user.username,
            reader_id=body.reader_id,
        )
    except service.CircError as exc:
        raise _http(exc) from exc


@router.post("/checkin")
def checkin(
    body: ItemActionIn,
    principal: Principal = Depends(require("circulation.checkin")),
    db: Session = Depends(get_db),
    connector: ResilientConnector = Depends(get_connector),
    devices: DeviceRegistry = Depends(get_devices),
    config: CirculationConfig = Depends(get_config),
):
    try:
        return service.checkin(
            db,
            connector,
            devices,
            config,
            item_ref=body.item_ref,
            actor=principal.user.username,
            reader_id=body.reader_id,
        )
    except service.CircError as exc:
        raise _http(exc) from exc


@router.get("/due")
def due_date(
    item_ref: str,
    _: Principal = Depends(desk_staff),
    db: Session = Depends(get_db),
    connector: ResilientConnector = Depends(get_connector),
):
    try:
        return service.due_date(db, connector, item_ref)
    except service.CircError as exc:
        raise _http(exc) from exc


# ------------------------------------------------------------------ lookups and history
@router.get("/items/resolve")
def resolve_item(
    ref: str,
    _: Principal = Depends(desk_staff),
    db: Session = Depends(get_db),
):
    """Name an item by accession no., barcode or tag UID and see whether it is on loan."""
    try:
        item = service.resolve_item(db, ref)
    except service.CircError as exc:
        raise _http(exc) from exc
    loan = service.open_loan(db, item.id)
    is_reference = item.is_reference or item.item_type == "reference"
    return {
        "accession_no": item.accession_no,
        "title": item.bib.title,
        "author": item.bib.author,
        "item_type": item.item_type,
        "status": item.status,
        "is_reference": is_reference,
        "loan": service.loan_out(loan) if loan else None,
    }


@router.get("/loans")
def list_loans(
    member_no: str = "",
    open_only: bool = True,
    limit: int = 25,
    offset: int = 0,
    _: Principal = Depends(history_viewer),
    db: Session = Depends(get_db),
):
    return service.list_loans(db, member_no=member_no, open_only=open_only, limit=min(limit, 100), offset=offset)


@router.get("/events")
def list_events(
    member_no: str = "",
    item_ref: str = "",
    limit: int = 30,
    offset: int = 0,
    _: Principal = Depends(history_viewer),
    db: Session = Depends(get_db),
):
    return service.list_events(db, member_no=member_no, item_ref=item_ref, limit=min(limit, 100), offset=offset)


# ------------------------------------------------------------------ development simulator (404 in production)
class PresentIn(BaseModel):
    item_ref: str = Field(min_length=1, max_length=64)
    reader_id: str = "staff-1"


class FailIn(BaseModel):
    mode: str = "timeout"
    times: int = Field(default=1, ge=1, le=10)


def _simulator(settings: Settings) -> None:
    if settings.environment == "production":
        raise HTTPException(404, "Not found")


@router.get("/mock/patron-cards", tags=["circulation-simulator"])
def mock_patron_cards(
    _: Principal = Depends(desk_staff), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    _simulator(settings)
    rows = db.execute(
        select(Card.uid, Member.member_no, Member.name, Member.status)
        .join(Member, Member.member_no == Card.member_ref)
        .where(Card.kind == "patron")
        .order_by(Member.member_no)
    ).all()
    return [{"uid": u, "member_no": n, "name": nm, "status": st} for u, n, nm, st in rows]


@router.post("/mock/present", tags=["circulation-simulator"])
def mock_present_item(
    body: PresentIn,
    _: Principal = Depends(desk_staff),
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    """Hold the item's tag up to the simulated reader, as staff would with the book in hand."""
    _simulator(settings)
    reader = devices.get(body.reader_id)
    if not isinstance(reader, MockStaffReader):
        raise HTTPException(404, "Not found")
    try:
        item = service.resolve_item(db, body.item_ref)
    except service.CircError as exc:
        raise _http(exc) from exc
    tag = active_tag(db, item.id)
    if tag is None:
        return {"on_reader": False, "message": "This item has no tag to hold up to the reader"}
    reader.place_tag(tag.tag_uid, replace=True)
    return {"on_reader": True, "uid": tag.tag_uid, "message": "Tag is on the reader"}


def _mock_ilms(connector: ResilientConnector, settings: Settings) -> MockIlms:
    _simulator(settings)
    if not isinstance(connector.inner, MockIlms):
        raise HTTPException(404, "Not found")
    return connector.inner


@router.get("/mock/ilms/fail", tags=["circulation-simulator"])
def mock_ilms_pending(
    _: Principal = Depends(desk_staff),
    connector: ResilientConnector = Depends(get_connector),
    settings: Settings = Depends(get_settings),
):
    return {"pending": _mock_ilms(connector, settings).pending_failures()}


@router.delete("/mock/ilms/fail", tags=["circulation-simulator"])
def mock_ilms_clear(
    _: Principal = Depends(desk_staff),
    connector: ResilientConnector = Depends(get_connector),
    settings: Settings = Depends(get_settings),
):
    """Forget any simulated failures still waiting."""
    _mock_ilms(connector, settings).clear_failures()
    return {"pending": 0}


@router.post("/mock/ilms/fail", tags=["circulation-simulator"])
def mock_ilms_fail(
    body: FailIn,
    _: Principal = Depends(desk_staff),
    connector: ResilientConnector = Depends(get_connector),
    settings: Settings = Depends(get_settings),
):
    """Make the mock library system fail its next requests (timeout, error or lost_reply) to see the retry logic."""
    _simulator(settings)
    if not isinstance(connector.inner, MockIlms):
        raise HTTPException(404, "Not found")
    try:
        connector.inner.fail_next(body.mode, body.times)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"status": "armed", "mode": body.mode, "times": body.times, "pending": connector.inner.pending_failures()}
