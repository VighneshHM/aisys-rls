"""Shelf inventory: sessions, handheld sweeps, and the found / missing / misplaced report.

A session is one location (shelf area). Each sweep reads every tag in range; the same tag may come back many times in
one sweep and across sweeps, so tags are de-duplicated per session and only a read counter grows. Every distinct tag
is classified against the catalogue and earns a confirmation signal on the handheld (beep + light):

    found            the item belongs in this location and is where it should be          ok       one beep, green
    shelved_on_loan  it belongs here but is recorded as on loan (returned and not checked in) warning  two beeps, amber
    misplaced        a known item that belongs in another location                         warning  two beeps, amber
    unknown          not an active tag of any item (never registered, removed or retired)  error    long beep, red

Missing = items that should be on this shelf (right location, available, tagged) and were not seen. Items on loan are
not expected on the shelf, and untagged items cannot be read, so both are reported as counts rather than as missing.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import Counter
from typing import cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.models import InventoryScan, InventorySession, Item, RfidTag, utcnow
from aisys.rfid.devices import DeviceError, DeviceUnavailable, Handheld
from aisys.rfid.registry import DeviceRegistry
from aisys.rfid.tagging import state_of, tag_links
from aisys.shelves import service as shelves
from aisys.timeutil import iso

log = logging.getLogger("aisys.inventory")
SIGNAL = {"found": "ok", "shelved_on_loan": "warning", "misplaced": "warning", "unknown": "error"}
RESULTS = tuple(SIGNAL)


class InventoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ------------------------------------------------------------------ locations and sessions
def locations(db: Session) -> list[dict]:
    """Every shelf location in the catalogue with what is expected there."""
    tagged = select(RfidTag.item_id).where(RfidTag.status == "active")  # items that currently carry a tag
    rows = db.execute(
        select(
            Item.location,
            func.count(Item.id),
            func.sum(case((Item.id.in_(tagged), 1), else_=0)),
            func.sum(case((Item.status == "on_loan", 1), else_=0)),
        )
        .where(Item.location != "")
        .group_by(Item.location)
        .order_by(Item.location)
    ).all()
    return [{"location": loc, "items": n, "tagged": int(t or 0), "on_loan": int(o or 0)} for loc, n, t, o in rows]


def get_session(db: Session, session_id: int) -> InventorySession:
    session = db.get(InventorySession, session_id)
    if session is None:
        raise InventoryError(404, f"No inventory session #{session_id}")
    return session


def session_out(s: InventorySession) -> dict:
    return {
        "id": s.id,
        "location": s.location,
        "handheld_id": s.handheld_id,
        "status": s.status,
        "started_by": s.started_by,
        "started_at": iso(s.started_at),
        "closed_by": s.closed_by,
        "closed_at": iso(s.closed_at),
        "sweeps": s.sweeps,
        "summary": (s.summary or {}).get("counts", {}),
    }


def start(db: Session, registry: DeviceRegistry, location: str, handheld_id: str, *, actor: str, ip: str = "") -> dict:
    location = location.strip()
    if not db.scalar(select(func.count(Item.id)).where(Item.location == location)):
        raise InventoryError(404, f"No items are recorded in the location '{location}'")
    if registry.get(handheld_id, "handheld") is None:
        raise InventoryError(404, f"Unknown handheld reader '{handheld_id}'")
    session = InventorySession(location=location, handheld_id=handheld_id, started_by=actor)
    db.add(session)
    db.flush()
    audit.record(
        db,
        actor=actor,
        action="inventory.start",
        entity="inventory_session",
        entity_id=session.id,
        detail={"location": location, "handheld": handheld_id},
        ip=ip,
    )
    db.commit()
    return session_out(session)


def list_sessions(db: Session, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(InventorySession).order_by(InventorySession.id.desc()).limit(limit))
    return [session_out(s) for s in rows]


# ------------------------------------------------------------------ sweeping
def _classify(db: Session, session: InventorySession, uid: str) -> tuple[str, Item | None, str]:
    """(result, item, note) for one tag."""
    links = tag_links(db, uid)
    active = next((link for link in links if link.status == "active"), None)
    if active is None:
        state = state_of(links)
        note = {
            "unknown": "Not registered to any item",
            "removed": "Tag was taken off an item and is not on one now",
            "retired": "Retired tag: out of service",
        }[state]
        return "unknown", None, note
    item = active.item
    if item.location != session.location:
        return "misplaced", item, f"Belongs in {item.location or 'no recorded location'}"
    if item.status == "on_loan":
        return "shelved_on_loan", item, "Recorded as on loan but found on the shelf: check it in"
    return "found", item, ""


def scan(db: Session, registry: DeviceRegistry, session_id: int, *, actor: str) -> dict:
    """One sweep with the session's handheld. Returns what was newly seen and the signals it earned."""
    session = get_session(db, session_id)
    if session.status != "open":
        raise InventoryError(409, "This inventory session is closed")
    handheld = cast("Handheld | None", registry.get(session.handheld_id, "handheld"))
    if handheld is None:
        raise InventoryError(404, f"Handheld reader '{session.handheld_id}' is not configured")
    try:
        observed = handheld.scan()
    except DeviceUnavailable as exc:
        raise InventoryError(503, f"The handheld '{session.handheld_id}' is offline") from exc
    except DeviceError as exc:
        raise InventoryError(502, f"Handheld error: {exc}") from exc

    reads = Counter(o.uid for o in observed)
    existing = {s.tag_uid: s for s in db.scalars(select(InventoryScan).where(InventoryScan.session_id == session.id))}
    now, news = utcnow(), []
    for uid, n in reads.items():
        result, item, note = _classify(db, session, uid)
        row = existing.get(uid)
        changed = row is None or row.result != result
        if row is None:
            row = InventoryScan(session_id=session.id, tag_uid=uid, first_seen=now, reads=0)
            db.add(row)
        row.item_id = item.id if item else None
        row.result, row.signal, row.note = result, SIGNAL[result], note
        row.home_location = item.location if item else ""
        row.reads += n
        row.last_seen = now
        if changed:
            news.append(
                {
                    "uid": uid,
                    "accession_no": item.accession_no if item else None,
                    "title": item.bib.title if item else None,
                    "result": result,
                    "signal": SIGNAL[result],
                    "note": note,
                }
            )
            try:  # the confirmation a person would hear and see; a dead buzzer must never lose the scan
                handheld.indicate(SIGNAL[result])
            except DeviceError:
                log.warning("could not signal '%s' on %s", SIGNAL[result], session.handheld_id)
    session.sweeps += 1
    db.commit()
    total, distinct = sum(reads.values()), len(reads)
    return {
        "sweep": session.sweeps,
        "reads": total,
        "distinct": distinct,
        "duplicate_reads": total - distinct,
        "new": news,
        "signals": dict(Counter(n["signal"] for n in news)),
        "counts": report(db, session.id)["counts"],
    }


# ------------------------------------------------------------------ the report
def _entry(item: Item, tag_uid: str, **extra) -> dict:
    return {
        "accession_no": item.accession_no,
        "title": item.bib.title,
        "call_number": item.call_number,
        "location": item.location,
        "tag_uid": tag_uid,
        **extra,
    }


def _shelf_order(db: Session, session: InventorySession) -> dict:
    """Books the sweep read out of call-number order (they need moving), from the order the handheld read them."""
    ids = shelves.out_of_order_items(db, session.id, session.location)
    checked = db.scalar(
        select(func.count(InventoryScan.id)).where(
            InventoryScan.session_id == session.id, InventoryScan.result.in_(("found", "shelved_on_loan"))
        )
    )
    books = []
    for item_id in ids:
        item = db.get(Item, item_id)
        assert item is not None  # scans only ever reference existing items
        books.append({"accession_no": item.accession_no, "title": item.bib.title, "call_number": item.call_number})
    return {"checked": checked or 0, "out_of_order": books, "in_order": not books}


def report(db: Session, session_id: int) -> dict:
    session = get_session(db, session_id)
    scans = list(
        db.scalars(select(InventoryScan).where(InventoryScan.session_id == session.id).order_by(InventoryScan.id))
    )
    seen_items = {s.item_id for s in scans if s.item_id is not None}
    expected = db.execute(
        select(Item, RfidTag.tag_uid)
        .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
        .where(Item.location == session.location, Item.status == "available")
        .order_by(Item.accession_no)
    ).all()
    items: dict[int | None, Item | None] = {s.item_id: db.get(Item, s.item_id) for s in scans if s.item_id is not None}
    by_result: dict[str, list[dict]] = {r: [] for r in RESULTS}
    for s in scans:
        item = items.get(s.item_id)
        entry = (
            _entry(item, s.tag_uid, home_location=s.home_location)
            if item
            else {"accession_no": None, "title": None, "call_number": "", "location": "", "tag_uid": s.tag_uid}
        )
        by_result[s.result].append(
            {**entry, "signal": s.signal, "note": s.note, "reads": s.reads, "last_seen": iso(s.last_seen)}
        )
    missing = [_entry(item, uid) for item, uid in expected if item.id not in seen_items]
    untagged = db.scalar(
        select(func.count(Item.id)).where(
            Item.location == session.location,
            Item.status == "available",
            ~Item.id.in_(select(RfidTag.item_id).where(RfidTag.status == "active")),
        )
    )
    on_loan = db.scalar(select(func.count(Item.id)).where(Item.location == session.location, Item.status == "on_loan"))
    counts = {
        "expected": len(expected),
        "found": len(by_result["found"]),
        "missing": len(missing),
        "misplaced": len(by_result["misplaced"]),
        "shelved_on_loan": len(by_result["shelved_on_loan"]),
        "unknown": len(by_result["unknown"]),
        "untagged": untagged,
        "on_loan": on_loan,
        "reads": sum(s.reads for s in scans),
        "distinct_tags": len(scans),
    }
    live = {
        "session": session_out(session),
        "counts": counts,
        "missing": missing,
        "shelf_order": _shelf_order(db, session),
        **by_result,
    }
    if session.status == "closed" and session.summary.get("lists"):  # a closed session is a record: frozen as closed
        return {**live, "counts": session.summary["counts"], **session.summary["lists"]}
    return live


def close(db: Session, session_id: int, *, actor: str, ip: str = "") -> dict:
    session = get_session(db, session_id)
    if session.status != "open":
        raise InventoryError(409, "This inventory session is already closed")
    final = report(db, session.id)
    summary = final["counts"]
    lists = {k: final[k] for k in ("missing", "shelf_order", *RESULTS)}
    session.status, session.closed_by, session.closed_at = "closed", actor, utcnow()
    session.summary = {"counts": summary, "lists": lists}
    audit.record(
        db,
        actor=actor,
        action="inventory.close",
        entity="inventory_session",
        entity_id=session.id,
        detail={"location": session.location, **summary},
        ip=ip,
    )
    db.commit()
    return report(db, session.id)


# ------------------------------------------------------------------ CSV export
def _safe(value: object) -> str:
    """Stop spreadsheet formula injection: a cell starting with = + - @ is read by Excel as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(rep: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["status", "accession_no", "title", "call_number", "belongs_in", "tag_uid", "signal", "note", "reads"])
    for kind in ("found", "missing", "misplaced", "shelved_on_loan", "unknown"):
        for e in rep[kind]:
            w.writerow(
                [
                    _safe(v)
                    for v in (
                        kind,
                        e.get("accession_no"),
                        e.get("title"),
                        e.get("call_number"),
                        e.get("home_location") or e.get("location"),
                        e.get("tag_uid"),
                        e.get("signal", ""),
                        e.get("note", ""),
                        e.get("reads", 0),
                    )
                ]
            )
    return out.getvalue()
