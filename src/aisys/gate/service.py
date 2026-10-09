"""Gate workflow: read what passed, decide, sound the alarm, photograph it, queue the notifications.

The alarm decision uses ONLY the security bit read from the tag at the gate. It never calls the library system (the
ILMS connector), so the gate keeps protecting the collection while the ILMS is down (FR07: "read the security bit while
the LMS is offline"). The local catalogue and loan records are used afterwards, to give an event its meaning:

    bit ON,  item not on loan   -> unauthorised      ALARM
    bit ON,  item on loan       -> armed_on_loan     ALARM  (check-out could not switch the bit off)
    bit ON,  tag not an item    -> unissued          ALARM
    bit OFF, item on loan       -> authorised        normal exit
    bit OFF, item not on loan   -> bit_off           warning (switched off without a loan)
    bit OFF, tag not an item    -> unissued          warning
    bit unreadable              -> treated as ON (fail safe)
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aisys.gate.config import GateConfig
from aisys.models import GateEvent, GateImage, GatePassage, Item, Loan, Notification, utcnow
from aisys.notifications import prefs, queue
from aisys.notifications.messages import KIND_LABEL, gate_alert
from aisys.rfid.devices import DeviceError, DeviceUnavailable
from aisys.rfid.registry import DeviceRegistry
from aisys.rfid.tagging import state_of, tag_links
from aisys.timeutil import aware, iso

log = logging.getLogger("aisys.gate")
KINDS = tuple(KIND_LABEL)


class GateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def get_gate(registry: DeviceRegistry, gate_id: str):
    gate = registry.get(gate_id, "gate")
    if gate is None:
        raise GateError(404, f"Unknown gate '{gate_id}'")
    return gate


def pick_camera(registry: DeviceRegistry):
    return next((d for d in registry.all() if d.kind == "camera"), None)


def classify(armed: bool, item: Item | None, on_loan: bool) -> tuple[str, bool]:
    """(kind, alarm) from the security bit and what we know about the tag. See the module docstring."""
    if item is None:
        return "unissued", armed
    if armed:
        return ("armed_on_loan" if on_loan else "unauthorised"), True
    return ("authorised" if on_loan else "bit_off"), False


def event_out(e: GateEvent, *, has_image: bool = False) -> dict:
    return {
        "id": e.id,
        "ts": iso(e.ts),
        "passage_id": e.passage_id,
        "gate_id": e.gate_id,
        "kind": e.kind,
        "label": KIND_LABEL.get(e.kind, e.kind),
        "alarm": e.alarm,
        "tag_uid": e.tag_uid,
        "item_id": e.item_id,
        "accession_no": e.accession_no,
        "title": e.title,
        "loan_member_no": e.loan_member_no,
        "security_armed": e.security_armed,
        "on_loan": e.on_loan,
        "people": e.people,
        "decided_by": e.decided_by,
        "duplicate_of": e.duplicate_of,
        "camera_id": e.camera_id,
        "image_error": e.image_error,
        "note": e.note,
        "has_image": has_image,
    }


def _has_image(db: Session, event_id: int) -> bool:
    return (db.scalar(select(func.count()).select_from(GateImage).where(GateImage.event_id == event_id)) or 0) > 0


def process(db: Session, registry: DeviceRegistry, gate_id: str, config: GateConfig) -> list[dict]:
    """Collect the gate's passages and turn every tag into an event. Returns the events created."""
    gate = get_gate(registry, gate_id)
    try:
        passages = gate.poll()
    except DeviceUnavailable as exc:
        raise GateError(503, f"Gate '{gate_id}' is offline") from exc
    except DeviceError as exc:
        raise GateError(502, f"Gate error: {exc}") from exc
    camera = pick_camera(registry)
    created: list[GateEvent] = []
    for passage in passages:
        row = GatePassage(ts=passage.at, gate_id=passage.gate_id, people=passage.people)
        db.add(row)
        db.flush()
        for obs in {o.uid: o for o in passage.tags}.values():  # a tag read twice in one passage is one event
            created.append(_record(db, gate, camera, config, row, obs))
    db.commit()
    return [event_out(e, has_image=_has_image(db, e.id)) for e in created]


def poll_all(db: Session, registry: DeviceRegistry, config: GateConfig, *, include_mock: bool = False) -> dict:
    """Read every configured gate once: the production path from a real gate to events, alarms, snapshots and alerts.

    Run by the application's gate loop (`[gate] poll_seconds`), by `aisys gate-poll` and by the Gate events page.
    Simulator gates are skipped unless asked for: in development the simulator hands its passages over itself. One gate
    that is offline or fails is reported and does not stop the others.
    """
    out: dict = {"gates": {}, "events": 0, "alarms": 0}
    for device in registry.all():
        if device.kind != "gate" or (getattr(device, "is_mock", False) and not include_mock):
            continue
        try:
            events = process(db, registry, device.device_id, config)
        except GateError as exc:
            db.rollback()
            out["gates"][device.device_id] = {"ok": False, "error": exc.message}
            log.warning("gate %s could not be read: %s", device.device_id, exc.message)
            continue
        alarms = sum(1 for e in events if e["alarm"])
        out["gates"][device.device_id] = {"ok": True, "events": len(events), "alarms": alarms}
        out["events"] += len(events)
        out["alarms"] += alarms
    return out


def _record(db, gate, camera, config: GateConfig, passage: GatePassage, obs) -> GateEvent:
    armed = True if obs.security_armed is None else bool(obs.security_armed)  # unreadable: fail safe
    links = tag_links(db, obs.uid)
    active = next((link for link in links if link.status == "active"), None)
    item = active.item if active else None
    loan = db.scalar(select(Loan).where(Loan.item_id == item.id, Loan.returned_at.is_(None))) if item else None
    kind, alarm = classify(armed, item, loan is not None)

    notes = []
    if obs.security_armed is None:
        notes.append("Security bit could not be read; treated as on.")
    if item is None and links:
        notes.append(f"This tag was used before and is now {state_of(links)}.")
    if kind == "armed_on_loan":
        notes.append("The item is on loan but its tag was still armed: switch it off at the desk.")
    if kind == "bit_off":
        notes.append("The security bit was off but the item is not on loan: check how it was switched off.")

    since = utcnow() - timedelta(seconds=config.duplicate_window_s)
    earlier = None
    if alarm:
        earlier = db.scalar(
            select(GateEvent)
            .where(
                GateEvent.tag_uid == obs.uid,
                GateEvent.gate_id == passage.gate_id,
                GateEvent.alarm,
                GateEvent.ts >= since,
            )
            .order_by(GateEvent.id.desc())
        )
    fresh = alarm and earlier is None  # a first alarm gets a photo and messages; a repeat does not
    recipients = prefs.recipients(db) if fresh else []
    if fresh and not recipients:
        notes.append("No alert recipients are configured, so no message was queued.")

    snapshot, image_error = None, ""
    if fresh:
        what = item.accession_no if item else obs.uid
        caption = f"{passage.gate_id} ALARM\n{KIND_LABEL[kind]}\n{what}\n{(item.bib.title if item else '')[:38]}"
        if camera is None:
            image_error = "No camera is configured"
        else:
            try:
                snapshot = camera.capture(caption)
            except DeviceUnavailable:
                image_error = "The camera is offline"
            except DeviceError as exc:
                image_error = f"The camera failed: {exc}"[:200]

    event = GateEvent(
        ts=passage.ts,
        passage_id=passage.id,
        gate_id=passage.gate_id,
        kind=kind,
        alarm=alarm,
        tag_uid=obs.uid,
        item_id=item.id if item else None,
        accession_no=item.accession_no if item else "",
        title=item.bib.title if item else "",
        loan_member_no=loan.member.member_no if loan else "",
        security_armed=obs.security_armed,
        on_loan=loan is not None,
        people=passage.people,
        duplicate_of=earlier.id if earlier else None,
        camera_id=getattr(camera, "device_id", "") if fresh else "",
        image_error=image_error,
        note=" ".join(notes)[:300],
    )
    db.add(event)
    db.flush()
    if snapshot is not None:
        db.add(
            GateImage(
                event_id=event.id, camera_id=camera.device_id, content_type=snapshot.content_type, data=snapshot.data
            )
        )
        db.flush()
    _signal(gate, "alarm" if alarm else "ok" if kind == "authorised" else "warning")
    if fresh:
        view = event_out(event, has_image=snapshot is not None)
        when = aware(event.ts).astimezone().strftime("%d %b %Y %H:%M")
        for user, channel, address in recipients:
            subject, body = gate_alert(channel, view, when)
            queue.enqueue(
                db, channel=channel, recipient=address, subject=subject, body=body, user_id=user.id, event_id=event.id
            )
    return event


def _signal(gate, pattern: str) -> None:
    try:
        gate.indicate(pattern)
    except DeviceError:  # a dead siren must never lose the record
        log.warning("could not signal '%s' on gate %s", pattern, gate.device_id)


# ------------------------------------------------------------------ reading back
def get_event(db: Session, event_id: int) -> dict:
    e = db.get(GateEvent, event_id)
    if e is None:
        raise GateError(404, f"No gate event #{event_id}")
    out = event_out(e, has_image=_has_image(db, e.id))
    notes = db.scalars(select(Notification).where(Notification.event_id == e.id).order_by(Notification.id))
    out["notifications"] = [queue.notification_out(n) for n in notes]
    return out


def get_image(db: Session, event_id: int) -> GateImage:
    image = db.scalar(select(GateImage).where(GateImage.event_id == event_id))
    if image is None:
        raise GateError(404, "No CCTV image for this event")
    return image


def list_events(
    db: Session, *, kind: str = "", alarms_only: bool = False, q: str = "", limit: int = 25, offset: int = 0
) -> dict:
    stmt = select(GateEvent)
    if kind:
        stmt = stmt.where(GateEvent.kind == kind)
    if alarms_only:
        stmt = stmt.where(GateEvent.alarm)
    q = q.strip().lower().replace("%", "").replace("_", "")
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                func.lower(GateEvent.accession_no).like(like),
                func.lower(GateEvent.title).like(like),
                func.lower(GateEvent.tag_uid).like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(GateEvent.id.desc()).limit(limit).offset(offset)).all()
    with_image = (
        set(db.scalars(select(GateImage.event_id).where(GateImage.event_id.in_([e.id for e in rows]))))
        if rows
        else set()
    )
    return {"total": total, "events": [event_out(e, has_image=e.id in with_image) for e in rows]}


def summary(db: Session) -> dict:
    """Headline numbers for the gate page (and, later, the reports)."""
    alarms = db.scalar(
        select(func.count()).select_from(GateEvent).where(GateEvent.alarm, GateEvent.duplicate_of.is_(None))
    )
    return {
        "alarms": alarms,
        "unauthorised": db.scalar(
            select(func.count())
            .select_from(GateEvent)
            .where(GateEvent.kind == "unauthorised", GateEvent.duplicate_of.is_(None))
        ),
        "passages": db.scalar(select(func.count()).select_from(GatePassage)),
        "people": db.scalar(select(func.coalesce(func.sum(GatePassage.people), 0))),
        "queued": queue.counts(db)["queued"],
    }
