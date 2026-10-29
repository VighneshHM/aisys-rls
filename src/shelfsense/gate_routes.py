"""HTTP API for the security gate, CCTV snapshots and the notification queue (plus the development simulator)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from shelfsense import audit
from shelfsense.circulation.service import CircError, resolve_item
from shelfsense.config import Settings
from shelfsense.deps import Principal, client_ip, get_db, get_settings, require
from shelfsense.gate import service
from shelfsense.models import Notification, RfidTag
from shelfsense.notifications import prefs, queue, reminders
from shelfsense.notifications.providers import CHANNELS, MockProvider, NotificationProvider, address_hint
from shelfsense.rfid.mock import MockCamera, MockGate
from shelfsense.rfid.registry import DeviceRegistry

router = APIRouter(prefix="/api/gate", tags=["gate"])
notify_router = APIRouter(prefix="/api/notifications", tags=["notifications"])
CanView = Depends(require("gate.view"))
CanAdmin = Depends(require("admin.config"))
SAFE_IMAGE_TYPES = {"image/svg+xml", "image/png", "image/jpeg"}


def get_devices(request: Request) -> DeviceRegistry:
    return request.app.state.devices


def get_providers(request: Request) -> dict[str, NotificationProvider]:
    return request.app.state.providers


def _http(exc: service.GateError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


# ------------------------------------------------------------------ events
@router.get("/summary")
def summary(_: Principal = CanView, db: Session = Depends(get_db)):
    return service.summary(db)


@router.get("/devices")
def devices_list(_: Principal = CanView, devices: DeviceRegistry = Depends(get_devices)):
    return [d for d in devices.describe() if d["kind"] in ("gate", "camera")]


@router.get("/events")
def events(
    kind: str = "",
    alarms_only: bool = False,
    q: str = "",
    limit: int = 25,
    offset: int = 0,
    _: Principal = CanView,
    db: Session = Depends(get_db),
):
    return service.list_events(
        db, kind=kind, alarms_only=alarms_only, q=q, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )


@router.get("/events/{event_id}")
def event(event_id: int, _: Principal = CanView, db: Session = Depends(get_db)):
    try:
        return service.get_event(db, event_id)
    except service.GateError as exc:
        raise _http(exc) from exc


@router.get("/events/{event_id}/image")
def event_image(event_id: int, _: Principal = CanView, db: Session = Depends(get_db)):
    """The CCTV snapshot. Served so that nothing inside it can run: it is only ever shown in an image tag."""
    try:
        image = service.get_image(db, event_id)
    except service.GateError as exc:
        raise _http(exc) from exc
    content_type = image.content_type if image.content_type in SAFE_IMAGE_TYPES else "application/octet-stream"
    return Response(
        image.data,
        media_type=content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "private, no-store",  # a CCTV image must not outlive the session in a shared browser cache
        },
    )


@router.post("/poll")
def poll_gates(
    _: Principal = CanView,
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    """Read every real gate now (the gate loop does this by itself when `[gate] poll_seconds` is set). Simulator
    gates are not read here: in development they hand their passages over when someone walks through."""
    return service.poll_all(db, devices, settings.gate)


# ------------------------------------------------------------------ development simulator (404 in production)
class WalkIn(BaseModel):
    item_refs: list[str] = Field(default_factory=list, max_length=20)  # accession no., barcode or tag UID
    unregistered: int = Field(default=0, ge=0, le=10)  # tags that belong to no item
    people: int = Field(default=1, ge=1, le=50)
    bit: str = Field(default="as_recorded", pattern="^(as_recorded|on|off)$")  # the security bit on the item tags


def _simulator(settings: Settings) -> None:
    if settings.environment == "production":
        raise HTTPException(404, "Not found")


@router.post("/mock/gates/{gate_id}/walk", tags=["gate-simulator"])
def mock_walk(
    gate_id: str,
    body: WalkIn,
    request: Request,
    _: Principal = Depends(require("rfid.tag")),
    db: Session = Depends(get_db),
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    """Someone walks through the gate carrying these items. The gate reads each tag's security bit as we recorded it
    (what check-out switched off), unless `bit` forces it. Returns the events the gate produced."""
    _simulator(settings)
    gate = devices.get(gate_id, "gate")
    if not isinstance(gate, MockGate):
        raise HTTPException(404, "Not found")
    tags: list[tuple[str, bool | None]] = []
    for ref in body.item_refs:
        try:
            item = resolve_item(db, ref)
        except CircError as exc:
            raise HTTPException(exc.status, detail={"message": exc.message, "problems": []}) from exc
        tag = db.scalar(select(RfidTag).where(RfidTag.item_id == item.id, RfidTag.status == "active"))
        if tag is None:
            raise HTTPException(
                409, detail={"message": f"{item.accession_no} has no tag, so the gate cannot see it", "problems": []}
            )
        armed = tag.security_armed if body.bit == "as_recorded" else body.bit == "on"
        tags.append((tag.tag_uid, armed))
    tags += [("E280" + secrets.token_hex(10).upper(), True) for _ in range(body.unregistered)]
    gate.walk_through(tags, body.people)
    try:
        return {"events": service.process(db, devices, gate_id, settings.gate)}
    except service.GateError as exc:
        raise _http(exc) from exc


@router.post("/mock/camera/fail", tags=["gate-simulator"])
def mock_camera_fail(
    _: Principal = Depends(require("rfid.tag")),
    devices: DeviceRegistry = Depends(get_devices),
    settings: Settings = Depends(get_settings),
):
    """Make the camera's next snapshot fail, to see an alarm recorded without a picture."""
    _simulator(settings)
    camera = service.pick_camera(devices)
    if not isinstance(camera, MockCamera):
        raise HTTPException(404, "Not found")
    camera.fail_next("capture")
    return {"status": "armed"}


# ------------------------------------------------------------------ notifications
@notify_router.get("")
def list_notifications(
    status: str = "",
    event_id: int | None = None,
    limit: int = 30,
    offset: int = 0,
    _: Principal = CanView,
    db: Session = Depends(get_db),
):
    return queue.list_notifications(
        db, status=status, event_id=event_id, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )


def _audit(db: Session, principal: Principal, request: Request, action: str, detail: dict, entity_id=None) -> None:
    """Every administrative action is recorded, including the hand-run notification ones."""
    audit.record(
        db, actor=principal.user.username, action=action, entity="notification", entity_id=entity_id, detail=detail,
        ip=client_ip(request),
    )  # fmt: skip
    db.commit()


@notify_router.post("/dispatch")
def dispatch(
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
    providers: dict[str, NotificationProvider] = Depends(get_providers),
    settings: Settings = Depends(get_settings),
):
    """Deliver every queued message that is due (failures are retried later, up to the configured limit)."""
    result = queue.dispatch(db, providers, settings.notifications)
    _audit(db, principal, request, "notification.dispatch", {k: result[k] for k in ("tried", "sent", "failed")})
    return result


@notify_router.post("/reminders/run")
def run_reminders(
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Queue the patron reminders that are due today (the background loop does this by itself)."""
    result = reminders.scan(db, settings.notifications, library_name=settings.app_name)
    _audit(db, principal, request, "notification.reminders_run", {"queued": result.get("queued", 0)})
    return result


@notify_router.post("/{notification_id}/retry")
def retry(notification_id: int, request: Request, principal: Principal = CanAdmin, db: Session = Depends(get_db)):
    if db.get(Notification, notification_id) is None:  # D-078: was "only a failed notification can be retried"
        raise HTTPException(404, detail={"message": "No such notification", "problems": []})
    row = queue.retry(db, notification_id)
    if row is None:
        raise HTTPException(409, detail={"message": "Only a failed notification can be retried", "problems": []})
    _audit(db, principal, request, "notification.retry", {"channel": row.channel}, entity_id=row.id)
    return queue.notification_out(row)


@notify_router.get("/outbox")
def outbox(_: Principal = CanAdmin, providers: dict[str, NotificationProvider] = Depends(get_providers)):
    """What the mock providers 'sent' (a real provider has its own log)."""
    sent = [m for p in providers.values() if isinstance(p, MockProvider) for m in p.outbox]
    return {"messages": sent[::-1][:50]}


class ChannelIn(BaseModel):
    active: bool = False
    address: str = Field(default="", max_length=200)


class PrefsIn(BaseModel):
    channels: dict[str, ChannelIn]


@notify_router.get("/prefs")
def get_prefs(_: Principal = CanAdmin, db: Session = Depends(get_db)):
    return {"users": prefs.list_prefs(db), "hints": {c: address_hint(c) for c in CHANNELS}}


@notify_router.put("/prefs/{user_id}")
def put_prefs(
    user_id: int,
    body: PrefsIn,
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
):
    try:
        return prefs.set_prefs(
            db,
            user_id,
            {c: s.model_dump() for c, s in body.channels.items()},
            actor=principal.user.username,
            ip=client_ip(request),
        )
    except prefs.PrefError as exc:
        raise HTTPException(exc.status, detail={"message": exc.message, "problems": []}) from exc


class ProviderFailIn(BaseModel):
    channel: str = Field(pattern="^(email|sms|print)$")
    mode: str = Field(default="error", pattern="^(error|timeout)$")
    times: int = Field(default=1, ge=1, le=10)


@notify_router.post("/mock/fail", tags=["notifications-simulator"])
def mock_fail(
    body: ProviderFailIn,
    _: Principal = CanAdmin,
    providers: dict[str, NotificationProvider] = Depends(get_providers),
    settings: Settings = Depends(get_settings),
):
    """Make a mock provider fail its next sends (to see retries). Use `times` 3 to exhaust the attempts."""
    _simulator(settings)
    provider = providers.get(body.channel)
    if not isinstance(provider, MockProvider):
        raise HTTPException(404, "Not found")
    provider.fail_next(body.mode, body.times)
    return {"status": "armed", "pending": provider.pending_failures()}


@notify_router.delete("/mock/fail", tags=["notifications-simulator"])
def mock_fail_clear(
    _: Principal = CanAdmin,
    providers: dict[str, NotificationProvider] = Depends(get_providers),
    settings: Settings = Depends(get_settings),
):
    _simulator(settings)
    for p in providers.values():
        if isinstance(p, MockProvider):
            p.clear_failures()
    return {"pending": 0}
