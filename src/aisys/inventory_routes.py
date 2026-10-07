"""HTTP API for inventory: sessions, handheld sweeps, reports, and the development simulator."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys.config import Settings
from aisys.deps import Principal, client_ip, get_db, get_settings, require
from aisys.inventory import service
from aisys.models import Item, RfidTag
from aisys.rfid.mock import MockHandheld
from aisys.rfid.registry import DeviceRegistry

router = APIRouter(prefix="/api/inventory", tags=["inventory"])
CanInventory = Depends(require("inventory.run"))


def get_devices(request: Request) -> DeviceRegistry:
    return request.app.state.devices


class StartIn(BaseModel):
    location: str = Field(min_length=1, max_length=100)
    handheld_id: str = "handheld-1"


def _http(exc: service.InventoryError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


@router.get("/locations")
def locations(_: Principal = CanInventory, db: Session = Depends(get_db)):
    return service.locations(db)


@router.get("/handhelds")
def handhelds(_: Principal = CanInventory, devices: DeviceRegistry = Depends(get_devices)):
    return [d for d in devices.describe() if d["kind"] == "handheld"]


@router.get("/sessions")
def sessions(limit: int = 20, _: Principal = CanInventory, db: Session = Depends(get_db)):
    return service.list_sessions(db, min(max(limit, 1), 100))


@router.post("/sessions", status_code=201)
def start_session(
    body: StartIn,
    request: Request,
    principal: Principal = CanInventory,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    try:
        return service.start(
            db, devices, body.location, body.handheld_id, actor=principal.user.username, ip=client_ip(request)
        )
    except service.InventoryError as exc:
        raise _http(exc) from exc


@router.get("/sessions/{session_id}")
def session_report(session_id: int, _: Principal = CanInventory, db: Session = Depends(get_db)):
    try:
        return service.report(db, session_id)
    except service.InventoryError as exc:
        raise _http(exc) from exc


@router.post("/sessions/{session_id}/scan")
def sweep(
    session_id: int,
    principal: Principal = CanInventory,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
):
    try:
        return service.scan(db, devices, session_id, actor=principal.user.username)
    except service.InventoryError as exc:
        raise _http(exc) from exc


@router.post("/sessions/{session_id}/close")
def close_session(
    session_id: int,
    request: Request,
    principal: Principal = CanInventory,
    db: Session = Depends(get_db),
):
    try:
        return service.close(db, session_id, actor=principal.user.username, ip=client_ip(request))
    except service.InventoryError as exc:
        raise _http(exc) from exc


@router.get("/sessions/{session_id}/export.csv")
def export_csv(session_id: int, _: Principal = CanInventory, db: Session = Depends(get_db)):
    try:
        rep = service.report(db, session_id)
    except service.InventoryError as exc:
        raise _http(exc) from exc
    name = f"inventory-{session_id}-{rep['session']['location']}".replace(" ", "_")
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    return Response(
        service.to_csv(rep),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
    )


# ------------------------------------------------------------------ development simulator (404 in production)
class LoadShelfIn(BaseModel):
    session_id: int
    missing: int = Field(default=1, ge=0, le=50)  # books that should be here but are left off the shelf
    strays: int = Field(default=1, ge=0, le=50)  # books that belong in other locations
    unknown: int = Field(default=1, ge=0, le=50)  # tags that belong to no item
    duplicates: int = Field(default=2, ge=0, le=50)  # tags read twice in a sweep


def _mock_handheld(handheld_id: str, devices: DeviceRegistry, settings: Settings) -> MockHandheld:
    reader = devices.get(handheld_id, "handheld")
    if settings.environment == "production" or not isinstance(reader, MockHandheld):
        raise HTTPException(404, "Not found")
    return reader


@router.post("/mock/handhelds/{handheld_id}/load-shelf", tags=["inventory-simulator"])
def mock_load_shelf(
    handheld_id: str,
    body: LoadShelfIn,
    _: Principal = CanInventory,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    """Put a believable shelf in range of the handheld, built from the catalogue, and say what was planted."""
    handheld = _mock_handheld(handheld_id, devices, settings)
    try:
        session = service.get_session(db, body.session_id)
    except service.InventoryError as exc:
        raise _http(exc) from exc
    here = db.execute(
        select(Item, RfidTag.tag_uid)
        .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
        .where(Item.location == session.location, Item.status == "available")
        .order_by(Item.accession_no)
    ).all()
    elsewhere = db.execute(
        select(Item, RfidTag.tag_uid)
        .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
        .where(Item.location != session.location, Item.status == "available")
        .order_by(Item.accession_no)
        .limit(body.strays)
    ).all()
    left_out = here[len(here) - body.missing :] if body.missing else []
    on_shelf = here[: len(here) - len(left_out)]
    unknown = [f"E280DEAD{n:08X}" for n in range(1, body.unknown + 1)]
    uids = [u for _, u in on_shelf] + [u for _, u in elsewhere] + unknown
    uids += [u for _, u in on_shelf[: body.duplicates]]  # read twice, as real hardware does
    handheld.load(uids)
    return {
        "in_range": len(uids),
        "left_off_the_shelf": [i.accession_no for i, _ in left_out],
        "strays": [{"accession_no": i.accession_no, "belongs_in": i.location} for i, _ in elsewhere],
        "unknown_tags": len(unknown),
        "duplicate_reads": min(body.duplicates, len(on_shelf)),
    }


@router.post("/mock/handhelds/{handheld_id}/power", tags=["inventory-simulator"])
def mock_power(
    handheld_id: str,
    online: bool,
    _: Principal = CanInventory,
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    _mock_handheld(handheld_id, devices, settings).online = online
    return {"online": online}
