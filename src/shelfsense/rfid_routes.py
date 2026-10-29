"""HTTP API for the RFID layer: readers, tagging workflow, tag lookup, monitoring, and the dev simulator."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelfsense.config import Settings
from shelfsense.deps import Principal, client_ip, get_db, get_settings, require
from shelfsense.models import TagEvent
from shelfsense.rfid import tagging
from shelfsense.rfid.events import normalize_tag_uid
from shelfsense.rfid.mock import MockStaffReader
from shelfsense.rfid.registry import DeviceRegistry

router = APIRouter(prefix="/api/rfid", tags=["rfid"])
CanTag = Depends(require("rfid.tag"))
CanRead = Depends(require("catalogue.read"))


def get_devices(request: Request) -> DeviceRegistry:
    return request.app.state.devices


class TagRequest(BaseModel):
    item_id: int
    tag_uid: str = Field(min_length=1, max_length=64)
    reader_id: str = "staff-1"
    replace: bool = False
    reason: str = Field(default="", max_length=200)
    # what happens to the item's old tag when `replace` is set: taken off (reusable) or out of service for good
    old_tag_fate: Literal["removed", "retired"] = "removed"


class PlaceTagIn(BaseModel):
    uid: str | None = Field(default=None, max_length=64)
    replace: bool = False  # True = replace: any tag already on the reader is removed first


class RetireIn(BaseModel):
    reason: str = Field(default="", max_length=200)
    reader_id: str | None = Field(
        default=None, max_length=64
    )  # reader to erase the tag on; defaults to where it was tagged


class PowerIn(BaseModel):
    online: bool


def _http(exc: tagging.TaggingError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": exc.problems})


# ------------------------------------------------------------------ devices
@router.get("/readers")
def list_readers(_: Principal = CanTag, devices: DeviceRegistry = Depends(get_devices)):
    return devices.describe()


@router.post("/readers/{reader_id}/read")
def read_reader(
    reader_id: str,
    principal: Principal = CanTag,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    try:
        return {
            "reader_id": reader_id,
            "tags": tagging.read_field(db, devices, reader_id, actor=principal.user.username),
        }
    except tagging.TaggingError as exc:
        raise _http(exc) from exc


# ------------------------------------------------------------------ development simulator (never in production)
def _mock(reader_id: str, devices: DeviceRegistry, settings: Settings) -> MockStaffReader:
    reader = devices.get(reader_id, "staff_reader")
    if settings.environment == "production" or not isinstance(reader, MockStaffReader):
        raise HTTPException(404, "Not found")  # indistinguishable from a missing route in production
    return reader


@router.post("/mock/readers/{reader_id}/tags", status_code=201, tags=["rfid-simulator"])
def mock_place_tag(
    reader_id: str,
    body: PlaceTagIn,
    _: Principal = CanTag,
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    reader = _mock(reader_id, devices, settings)
    try:
        uid = reader.place_tag(body.uid, replace=body.replace)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"uid": uid, "on_pad": len(reader.read_field())}


@router.delete("/mock/readers/{reader_id}/tags", tags=["rfid-simulator"])
def mock_clear_pad(
    reader_id: str,
    _: Principal = CanTag,
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    _mock(reader_id, devices, settings).clear_pad()
    return {"status": "cleared"}


@router.post("/mock/readers/{reader_id}/power", tags=["rfid-simulator"])
def mock_power(
    reader_id: str,
    body: PowerIn,
    _: Principal = CanTag,
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    _mock(reader_id, devices, settings).online = body.online
    return {"online": body.online}


# ------------------------------------------------------------------ tagging workflow
@router.post("/tagging/validate")
def validate(body: TagRequest, _: Principal = CanTag, db: Session = Depends(get_db)):
    """Dry run: every problem at once, nothing written."""
    return tagging.validate_tagging(db, body.item_id, body.tag_uid, body.replace, body.old_tag_fate).out()


@router.post("/tagging", status_code=201)
def tag_item(
    body: TagRequest,
    request: Request,
    principal: Principal = CanTag,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    try:
        return tagging.associate(
            db,
            devices,
            item_id=body.item_id,
            tag_uid=body.tag_uid,
            reader_id=body.reader_id,
            replace=body.replace,
            reason=body.reason,
            old_tag_fate=body.old_tag_fate,
            actor=principal.user.username,
            ip=client_ip(request),
        )
    except tagging.TaggingError as exc:
        raise _http(exc) from exc


@router.get("/tags/{tag_uid}")
def lookup_tag(tag_uid: str, _: Principal = CanRead, db: Session = Depends(get_db)):
    try:
        return tagging.tag_detail(db, tag_uid)
    except tagging.TaggingError as exc:
        raise _http(exc) from exc


@router.get("/tags")
def list_tags(
    state: Literal["active", "removed", "retired"] | None = None,
    q: str = "",
    limit: int = 25,
    offset: int = 0,
    _: Principal = CanRead,
    db: Session = Depends(get_db),
):
    """The tag registry: every physical tag with its state (active / removed = reusable / retired = permanent)."""
    return tagging.list_tags(db, state=state, q=q, limit=limit, offset=offset)


def _end(fate: str, tag_uid: str, body: RetireIn, request: Request, principal: Principal, db: Session, devices):
    try:
        return tagging.end_tag(
            db,
            devices,
            tag_uid,
            fate=fate,
            reason=body.reason,
            actor=principal.user.username,
            ip=client_ip(request),
            reader_id=body.reader_id,
        )
    except tagging.TaggingError as exc:
        raise _http(exc) from exc


@router.post("/tags/{tag_uid}/remove")
def remove(
    tag_uid: str,
    body: RetireIn,
    request: Request,
    principal: Principal = CanTag,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    """Take a tag off its item. The tag is erased if it is on the reader and can be linked again later."""
    return _end("removed", tag_uid, body, request, principal, db, devices)


@router.post("/tags/{tag_uid}/retire")
def retire(
    tag_uid: str,
    body: RetireIn,
    request: Request,
    principal: Principal = CanTag,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    """Take a tag out of service for good (damaged or lost). Also works on a tag that was already removed."""
    return _end("retired", tag_uid, body, request, principal, db, devices)


@router.get("/events")
def events(
    tag_uid: str | None = None,
    type: str | None = None,  # noqa: A002 - query parameter name
    limit: int = 50,
    _: Principal = CanTag,
    db: Session = Depends(get_db),
):
    q = select(TagEvent).order_by(TagEvent.id.desc()).limit(min(max(limit, 1), 500))
    if tag_uid:
        q = q.where(TagEvent.tag_uid == normalize_tag_uid(tag_uid))
    if type:
        q = q.where(TagEvent.event_type == type)
    return [tagging.event_out(e) for e in db.scalars(q)]
