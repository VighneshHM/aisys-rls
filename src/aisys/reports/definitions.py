"""The seven reports. Each is a query plus a few summary figures and one chart; all of them are read-only.

Dates are whole days in UTC. Every report applies the same filters to its table, its summary figures and its chart, so
what you see at the top always adds up to what is listed below.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select

from aisys.models import (
    AuditEvent,
    BibRecord,
    Card,
    CircEvent,
    FineEntry,
    GateEvent,
    GateImage,
    GatePassage,
    InventoryScan,
    InventorySession,
    Item,
    Loan,
    Member,
    Notification,
    RfidTag,
    TagEvent,
    User,
)
from aisys.notifications.messages import KIND_LABEL
from aisys.reports.core import (
    Column,
    Context,
    Filter,
    Report,
    Result,
    choice,
    date_range,
    flag,
    like,
    per_day,
    text,
)
from aisys.timeutil import iso

CHART_TOP = 10


def _count(ctx: Context, stmt: Select) -> int:
    return ctx.db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0


def _page(stmt: Select, limit: int, offset: int) -> Select:
    return stmt.limit(limit).offset(offset) if limit > 0 else stmt


def _between(column, lo, hi) -> list:
    return ([column >= lo] if lo else []) + ([column < hi] if hi else [])


def _stat(label: str, value, kind: str = "number") -> dict:
    return {"label": label, "value": value, "kind": kind}


def _money_choices() -> tuple[tuple[str, str], ...]:
    return (
        ("charge", "Fines charged"),
        ("payment", "Payments"),
        ("waiver", "Waivers"),
        ("opening", "Opening balances"),
    )


# ================================================================== 1. tagged items
TAG_STATES = (("active", "In use"), ("removed", "Removed"), ("retired", "Retired"))


def tagged_items(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    state, location, by, q = choice(p, "state", TAG_STATES), text(p, "location"), text(p, "tagged_by"), text(p, "q")
    conds = _between(RfidTag.tagged_at, lo, hi)
    if state:
        conds.append(RfidTag.status == state)
    if location:
        conds.append(func.lower(Item.location) == location.lower())
    if by:
        conds.append(func.lower(RfidTag.tagged_by) == by.lower())
    if q:
        k = like(q)
        conds.append(
            or_(
                func.lower(Item.accession_no).like(k),
                func.lower(BibRecord.title).like(k),
                func.lower(RfidTag.tag_uid).like(k),
            )
        )
    joined = (
        select(RfidTag, Item, BibRecord)
        .join(Item, Item.id == RfidTag.item_id)
        .join(BibRecord, BibRecord.id == Item.bib_id)
    )
    stmt = joined.where(*conds).order_by(RfidTag.id.desc())
    total = _count(ctx, stmt)
    rows = [
        {
            "accession_no": i.accession_no,
            "title": b.title,
            "location": i.location,
            "tag_uid": t.tag_uid,
            "state": dict(TAG_STATES)[t.status],
            "tagged_by": t.tagged_by,
            "tagged_at": iso(t.tagged_at),
            "ended_at": iso(t.ended_at),
            "end_reason": t.end_reason,
        }
        for t, i, b in ctx.db.execute(_page(stmt, limit, offset)).all()
    ]
    by_state = dict(
        ctx.db.execute(
            select(RfidTag.status, func.count())
            .join(Item, Item.id == RfidTag.item_id)
            .join(BibRecord, BibRecord.id == Item.bib_id)
            .where(*conds)
            .group_by(RfidTag.status)
        ).all()
    )
    tagged_items_n = (
        ctx.db.scalar(select(func.count(func.distinct(RfidTag.item_id))).where(RfidTag.status == "active")) or 0
    )
    all_items = ctx.db.scalar(select(func.count(Item.id))) or 0
    by_location = ctx.db.execute(
        select(Item.location, func.count())
        .select_from(RfidTag)
        .join(Item, Item.id == RfidTag.item_id)
        .join(BibRecord, BibRecord.id == Item.bib_id)
        .where(*conds)
        .group_by(Item.location)
        .order_by(func.count().desc())
        .limit(CHART_TOP)
    ).all()
    summary = [
        _stat("Tag links", total),
        _stat("In use", by_state.get("active", 0)),
        _stat("Removed", by_state.get("removed", 0)),
        _stat("Retired", by_state.get("retired", 0)),
        _stat(
            "Items with a tag",
            f"{tagged_items_n:,} of {all_items:,}"
            + (f" ({round(100 * tagged_items_n / all_items)}%)" if all_items else ""),
            "text",
        ),
    ]
    chart = {
        "title": "Tag links by location",
        "unit": "number",
        "points": [{"label": loc or "(no location)", "value": n} for loc, n in by_location],
    }
    return Result(rows, total, summary, chart)


# ================================================================== 2. members
def _member_state(status: str):
    """The WHERE for a member status filter: removed members are stored as blocked, so the two must be told apart."""
    if status == "removed":
        return Member.removed_at.is_not(None)
    return and_(Member.status == status, Member.removed_at.is_(None))


def members(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    status = choice(p, "status", (("active", "Active"), ("blocked", "Blocked"), ("removed", "Removed")))
    owes = choice(p, "owes", (("yes", "Owes something"), ("over", "Over the fine limit")))
    q = text(p, "q")
    now = datetime.now(UTC)
    open_loans = (
        select(func.count(Loan.id))
        .where(Loan.member_id == Member.id, Loan.returned_at.is_(None))
        .correlate(Member)
        .scalar_subquery()
    )
    overdue = (
        select(func.count(Loan.id))
        .where(Loan.member_id == Member.id, Loan.returned_at.is_(None), Loan.due_at < now)
        .correlate(Member)
        .scalar_subquery()
    )
    card = (
        select(Card.uid)
        .where(Card.kind == "patron", Card.member_ref == Member.member_no, Card.active)
        .correlate(Member)
        .limit(1)
        .scalar_subquery()
    )
    conds = [_member_state(status)] if status else [Member.removed_at.is_(None)]  # removed ones only when asked for
    if owes == "yes":
        conds.append(Member.fines_cents > 0)
    if owes == "over":
        conds.append(Member.fines_cents > ctx.config.fine_limit_cents)
    if flag(p, "has_overdue"):
        conds.append(overdue > 0)
    if q:
        k = like(q)
        conds.append(or_(func.lower(Member.name).like(k), func.lower(Member.member_no).like(k)))
    stmt = select(Member, open_loans, overdue, card).where(*conds).order_by(Member.member_no)
    total = _count(ctx, stmt)
    rows = [
        {
            "member_no": m.member_no,
            "name": m.name,
            "status": ("Removed" if m.removed_at is not None else m.status.capitalize()),
            "fines_cents": m.fines_cents,
            "open_loans": o,
            "overdue_loans": d,
            "card": c or "",
        }
        for m, o, d, c in ctx.db.execute(_page(stmt, limit, offset)).all()
    ]
    agg = ctx.db.execute(
        select(
            func.sum(case((Member.status == "blocked", 1), else_=0)),
            func.sum(case((Member.fines_cents > 0, 1), else_=0)),
            func.coalesce(func.sum(Member.fines_cents), 0),
        ).where(*conds)
    ).one()
    top = ctx.db.execute(
        select(Member.name, Member.fines_cents)
        .where(*conds, Member.fines_cents > 0)
        .order_by(Member.fines_cents.desc())
        .limit(CHART_TOP)
    ).all()
    summary = [
        _stat("Members", total),
        _stat("Blocked", agg[0] or 0),
        _stat("Owing", agg[1] or 0),
        _stat("Total owed", agg[2], "money"),
    ]
    chart = {"title": "Largest balances", "unit": "money", "points": [{"label": n, "value": v} for n, v in top]}
    return Result(rows, total, summary, chart)


# ================================================================== 3. circulation
ACTIONS = (("checkout", "Check-out"), ("renew", "Renewal"), ("checkin", "Check-in"))
OUTCOMES = (("ok", "Done"), ("refused", "Refused"), ("failed", "Failed (library system)"))


def circulation(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    action, outcome = choice(p, "action", ACTIONS), choice(p, "outcome", OUTCOMES)
    operator, member_no, q = text(p, "operator"), text(p, "member_no").upper(), text(p, "q")
    conds = _between(CircEvent.ts, lo, hi)
    if action:
        conds.append(CircEvent.action == action)
    if outcome:
        conds.append(CircEvent.outcome == outcome)
    if operator:
        conds.append(func.lower(CircEvent.actor) == operator.lower())
    if member_no:
        conds.append(Member.member_no == member_no)
    if q:
        k = like(q)
        conds.append(or_(func.lower(Item.accession_no).like(k), func.lower(BibRecord.title).like(k)))

    def base(*cols):
        return (
            select(*cols)
            .select_from(CircEvent)
            .outerjoin(Member, Member.id == CircEvent.member_id)
            .outerjoin(Item, Item.id == CircEvent.item_id)
            .outerjoin(BibRecord, BibRecord.id == Item.bib_id)
            .where(*conds)
        )

    stmt = base(CircEvent, Member.member_no, Item.accession_no, BibRecord.title).order_by(CircEvent.id.desc())
    total = _count(ctx, stmt)
    rows = [
        {
            "ts": iso(e.ts),
            "action": dict(ACTIONS).get(e.action, e.action),
            "outcome": dict(OUTCOMES).get(e.outcome, e.outcome),
            "code": e.code,
            "accession_no": acc or "",
            "title": title or "",
            "member_no": no or "",
            "actor": e.actor,
            "message": e.message,
        }
        for e, no, acc, title in ctx.db.execute(_page(stmt, limit, offset)).all()
    ]
    by_outcome = dict(ctx.db.execute(base(CircEvent.outcome, func.count()).group_by(CircEvent.outcome)).all())
    distinct = ctx.db.execute(
        base(func.count(func.distinct(CircEvent.member_id)), func.count(func.distinct(CircEvent.item_id)))
    ).one()
    days = dict(ctx.db.execute(base(func.date(CircEvent.ts), func.count()).group_by(func.date(CircEvent.ts))).all())
    summary = [
        _stat("Transactions", total),
        _stat("Done", by_outcome.get("ok", 0)),
        _stat("Refused", by_outcome.get("refused", 0)),
        _stat("Failed", by_outcome.get("failed", 0)),
        _stat("Members", distinct[0]),
        _stat("Items", distinct[1]),
    ]
    chart = {"title": "Transactions per day", "unit": "number", "points": per_day(days, lo, hi)}
    return Result(rows, total, summary, chart)


# ================================================================== 4. operators
def operators(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    only = text(p, "operator").lower()
    db = ctx.db
    circ: dict[str, dict[str, int]] = {}
    for actor, action, outcome, n in db.execute(
        select(CircEvent.actor, CircEvent.action, CircEvent.outcome, func.count())
        .where(*_between(CircEvent.ts, lo, hi))
        .group_by(CircEvent.actor, CircEvent.action, CircEvent.outcome)
    ):
        c = circ.setdefault(actor, {})
        key = action if outcome == "ok" else "refused"
        c[key] = c.get(key, 0) + n
    tags = dict(
        db.execute(
            select(TagEvent.actor, func.count())
            .where(TagEvent.event_type.in_(["tagged", "retagged"]), *_between(TagEvent.ts, lo, hi))
            .group_by(TagEvent.actor)
        ).all()
    )
    sessions = dict(
        db.execute(
            select(InventorySession.started_by, func.count())
            .where(*_between(InventorySession.started_at, lo, hi))
            .group_by(InventorySession.started_by)
        ).all()
    )
    money: dict[str, dict[str, int]] = {}
    for actor, kind, n in db.execute(
        select(FineEntry.actor, FineEntry.kind, func.count())
        .where(FineEntry.kind.in_(["payment", "waiver"]), *_between(FineEntry.ts, lo, hi))
        .group_by(FineEntry.actor, FineEntry.kind)
    ):
        money.setdefault(actor, {})[kind] = n
    rows = []
    for u in db.scalars(select(User).where(User.active).order_by(User.username)):
        if only and u.username.lower() != only:
            continue
        c, m = circ.get(u.username, {}), money.get(u.username, {})
        row: dict[str, Any] = {
            "username": u.username,
            "name": u.display_name,
            "role": u.role.name.replace("_", " "),
            "checkouts": c.get("checkout", 0),
            "renewals": c.get("renew", 0),
            "checkins": c.get("checkin", 0),
            "refused": c.get("refused", 0),
            "tags_linked": tags.get(u.username, 0),
            "inventory_sessions": sessions.get(u.username, 0),
            "payments": m.get("payment", 0),
            "waivers": m.get("waiver", 0),
        }
        row["total"] = sum(v for k, v in row.items() if isinstance(v, int))
        rows.append(row)
    rows.sort(key=lambda r: (-r["total"], r["username"]))
    busiest = rows[0] if rows and rows[0]["total"] else None
    summary = [
        _stat("Staff", len(rows)),
        _stat("Active in this period", sum(1 for r in rows if r["total"])),
        _stat("Actions", sum(r["total"] for r in rows)),
        _stat("Busiest", f"{busiest['name']} ({busiest['total']})" if busiest else "-", "text"),
    ]
    chart = {
        "title": "Actions per person",
        "unit": "number",
        "points": [{"label": r["name"], "value": r["total"]} for r in rows[:CHART_TOP]],
    }
    window = rows[offset : offset + limit] if limit > 0 else rows
    return Result(window, len(rows), summary, chart)


# ================================================================== 5. RFID clients (devices)
DEVICE_KINDS = (
    ("staff_reader", "Staff readers"),
    ("handheld", "Handhelds"),
    ("gate", "Gates"),
    ("camera", "Cameras"),
    ("card_printer", "Card printers"),
)


def rfid_clients(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    kind = choice(p, "kind", DEVICE_KINDS)
    db = ctx.db

    def last(column, *where):
        return db.scalar(select(func.max(column)).where(*where))

    rows = []
    for d in ctx.devices.describe():
        if kind and d["kind"] != kind:
            continue
        did, activity, unit, detail, seen = d["id"], 0, "", "", None
        if d["kind"] == "staff_reader":
            counts = dict(
                db.execute(
                    select(TagEvent.event_type, func.count())
                    .where(TagEvent.reader_id == did, *_between(TagEvent.ts, lo, hi))
                    .group_by(TagEvent.event_type)
                ).all()
            )
            activity, unit = sum(counts.values()), "tag events"
            linked = counts.get("tagged", 0) + counts.get("retagged", 0)
            ended = counts.get("removed", 0) + counts.get("retired", 0)
            detail = f"{counts.get('read', 0)} reads, {linked} linked, {ended} taken off"
            seen = last(TagEvent.ts, TagEvent.reader_id == did)
        elif d["kind"] == "handheld":
            sess = db.execute(
                select(func.count(), func.coalesce(func.sum(InventorySession.sweeps), 0)).where(
                    InventorySession.handheld_id == did, *_between(InventorySession.started_at, lo, hi)
                )
            ).one()
            tags_seen = db.scalar(
                select(func.count(InventoryScan.id))
                .join(InventorySession, InventorySession.id == InventoryScan.session_id)
                .where(InventorySession.handheld_id == did, *_between(InventorySession.started_at, lo, hi))
            )
            activity, unit, detail = sess[1], "sweeps", f"{sess[0]} sessions, {tags_seen} distinct tags"
            seen = last(InventorySession.started_at, InventorySession.handheld_id == did)
        elif d["kind"] == "gate":
            ps = db.execute(
                select(func.count(), func.coalesce(func.sum(GatePassage.people), 0)).where(
                    GatePassage.gate_id == did, *_between(GatePassage.ts, lo, hi)
                )
            ).one()
            alarms = db.scalar(
                select(func.count(GateEvent.id)).where(
                    GateEvent.gate_id == did,
                    GateEvent.alarm,
                    GateEvent.duplicate_of.is_(None),
                    *_between(GateEvent.ts, lo, hi),
                )
            )
            activity, unit, detail = ps[0], "passages", f"{ps[1]} people, {alarms} alarms"
            seen = last(GatePassage.ts, GatePassage.gate_id == did)
        elif d["kind"] == "camera":
            activity = (
                db.scalar(
                    select(func.count(GateImage.id)).where(GateImage.camera_id == did, *_between(GateImage.ts, lo, hi))
                )
                or 0
            )
            unit, detail = "snapshots", "taken for gate alarms"
            seen = last(GateImage.ts, GateImage.camera_id == did)
        elif d["kind"] == "card_printer":
            mine = (AuditEvent.action == "member.card_personalise", AuditEvent.detail["printer"].as_string() == did)
            activity = (
                db.scalar(
                    select(func.count(AuditEvent.id)).where(
                        *mine, AuditEvent.outcome == "success", *_between(AuditEvent.ts, lo, hi)
                    )
                )
                or 0
            )
            jams = db.scalar(
                select(func.count(AuditEvent.id)).where(
                    *mine, AuditEvent.outcome == "failure", *_between(AuditEvent.ts, lo, hi)
                )
            )
            unit, detail = "cards printed", f"{jams} failed"
            seen = last(AuditEvent.ts, *mine)
        rows.append(
            {
                "device": did,
                "kind": dict(DEVICE_KINDS).get(d["kind"], d["kind"]),
                "driver": d["driver"],
                "status": "Online" if d["online"] else "Offline",
                "activity": activity,
                "unit": unit,
                "detail": detail,
                "last_activity": iso(seen) if seen else None,
            }
        )
    summary = [
        _stat("Devices", len(rows)),
        _stat("Online", sum(1 for r in rows if r["status"] == "Online")),
        _stat("Offline", sum(1 for r in rows if r["status"] == "Offline")),
        _stat("Activity", sum(r["activity"] for r in rows)),
    ]
    chart = {
        "title": "Activity per device",
        "unit": "number",
        "points": [{"label": r["device"], "value": r["activity"]} for r in rows],
    }
    window = rows[offset : offset + limit] if limit > 0 else rows
    return Result(window, len(rows), summary, chart)


# ================================================================== 6. fines
def fines(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    kind, member_no, q = choice(p, "kind", _money_choices()), text(p, "member_no").upper(), text(p, "q")
    conds = _between(FineEntry.ts, lo, hi)
    if kind:
        conds.append(FineEntry.kind == kind)
    if member_no:
        conds.append(Member.member_no == member_no)
    if q:
        conds.append(func.lower(Member.name).like(like(q)))
    stmt = (
        select(FineEntry, Member)
        .join(Member, Member.id == FineEntry.member_id)
        .where(*conds)
        .order_by(FineEntry.id.desc())
    )
    total = _count(ctx, stmt)
    labels = dict(_money_choices())
    rows = [
        {
            "ts": iso(e.ts),
            "member_no": m.member_no,
            "name": m.name,
            "kind": labels[e.kind],
            "amount": e.amount_cents,
            "note": e.note,
            "actor": e.actor,
        }
        for e, m in ctx.db.execute(_page(stmt, limit, offset)).all()
    ]
    sums = dict(
        ctx.db.execute(
            select(FineEntry.kind, func.coalesce(func.sum(FineEntry.amount_cents), 0))
            .join(Member, Member.id == FineEntry.member_id)
            .where(*conds)
            .group_by(FineEntry.kind)
        ).all()
    )
    owed = ctx.db.scalar(select(func.coalesce(func.sum(Member.fines_cents), 0)))
    summary = [
        _stat("Charged", sums.get("charge", 0), "money"),
        _stat("Paid", sums.get("payment", 0), "money"),
        _stat("Waived", sums.get("waiver", 0), "money"),
        _stat("Opening balances", sums.get("opening", 0), "money"),
        _stat("Owed now (everyone)", owed, "money"),
    ]
    chart = {
        "title": "Amount by kind",
        "unit": "money",
        "points": [{"label": labels[k], "value": sums.get(k, 0)} for k, _ in _money_choices()],
    }
    return Result(rows, total, summary, chart)


# ================================================================== 7. gate events
def gate_events(ctx: Context, p: dict, limit: int, offset: int) -> Result:
    lo, hi = date_range(p)
    kind = choice(p, "kind", tuple((k, v) for k, v in KIND_LABEL.items()))
    gate, q = text(p, "gate"), text(p, "q")
    conds = _between(GateEvent.ts, lo, hi)
    if kind:
        conds.append(GateEvent.kind == kind)
    if gate:
        conds.append(func.lower(GateEvent.gate_id) == gate.lower())
    if flag(p, "alarms_only"):
        conds.append(GateEvent.alarm)
    if q:
        k = like(q)
        conds.append(
            or_(
                func.lower(GateEvent.accession_no).like(k),
                func.lower(GateEvent.title).like(k),
                func.lower(GateEvent.tag_uid).like(k),
            )
        )
    stmt = select(GateEvent).where(*conds).order_by(GateEvent.id.desc())
    total = _count(ctx, stmt)
    page = ctx.db.scalars(_page(stmt, limit, offset)).all()
    msgs: dict[int, dict[str, int]] = {}
    if page:
        for eid, st, n in ctx.db.execute(
            select(Notification.event_id, Notification.status, func.count())
            .where(Notification.event_id.in_([e.id for e in page]))
            .group_by(Notification.event_id, Notification.status)
        ):
            if eid is not None:
                msgs.setdefault(eid, {})[st] = n
    rows = [
        {
            "ts": iso(e.ts),
            "gate": e.gate_id,
            "kind": KIND_LABEL.get(e.kind, e.kind),
            "alarm": "ALARM" if e.alarm and not e.duplicate_of else "repeat" if e.alarm else "",
            "accession_no": e.accession_no,
            "title": e.title,
            "tag_uid": e.tag_uid,
            "people": e.people,
            "messages": ", ".join(f"{n} {s}" for s, n in sorted(msgs.get(e.id, {}).items())),
        }
        for e in page
    ]
    first_alarms = [*conds, GateEvent.alarm, GateEvent.duplicate_of.is_(None)]
    alarms = ctx.db.scalar(select(func.count(GateEvent.id)).where(*first_alarms)) or 0
    unauthorised = (
        ctx.db.scalar(select(func.count(GateEvent.id)).where(*first_alarms, GateEvent.kind == "unauthorised")) or 0
    )
    repeats = ctx.db.scalar(select(func.count(GateEvent.id)).where(*conds, GateEvent.duplicate_of.is_not(None))) or 0
    foot_conds = _between(GatePassage.ts, lo, hi) + ([func.lower(GatePassage.gate_id) == gate.lower()] if gate else [])
    people = ctx.db.scalar(select(func.coalesce(func.sum(GatePassage.people), 0)).where(*foot_conds))
    sent = dict(
        ctx.db.execute(
            select(Notification.status, func.count())
            .join(GateEvent, GateEvent.id == Notification.event_id)
            .where(*conds)
            .group_by(Notification.status)
        ).all()
    )
    days: dict[str, int] = dict(
        ctx.db.execute(
            select(func.date(GateEvent.ts), func.count()).where(*first_alarms).group_by(func.date(GateEvent.ts))
        ).all()
    )
    summary = [
        _stat("Events", total),
        _stat("Alarms", alarms),
        _stat("Unauthorised removals", unauthorised),
        _stat("Repeats", repeats),
        _stat("Footfall (people)", people),
        _stat("Messages sent", sent.get("sent", 0)),
        _stat("Messages waiting or failed", sent.get("queued", 0) + sent.get("failed", 0)),
    ]
    chart = {"title": "Alarms per day", "unit": "number", "points": per_day(days, lo, hi)}
    return Result(rows, total, summary, chart)


# ================================================================== the catalogue of reports
DATES = (Filter("from", "From", "date"), Filter("to", "To", "date"))
REPORTS: dict[str, Report] = {
    r.id: r
    for r in (
        Report(
            "tagged_items",
            "Tagged items",
            "Every link between a tag and an item, with its state and who made it.",
            (
                *DATES,
                Filter("state", "Tag state", "select", TAG_STATES),
                Filter("location", "Location", "text", placeholder="exact name"),
                Filter("tagged_by", "Tagged by", "text", placeholder="username"),
                Filter("q", "Search", "text", placeholder="accession, title or tag"),
            ),
            (
                Column("accession_no", "Accession"),
                Column("title", "Title"),
                Column("location", "Location"),
                Column("tag_uid", "Tag", "mono"),
                Column("state", "State", "badge"),
                Column("tagged_by", "Tagged by"),
                Column("tagged_at", "Tagged", "datetime"),
                Column("ended_at", "Ended", "datetime"),
                Column("end_reason", "Reason"),
            ),
            tagged_items,
        ),
        Report(
            "members",
            "Members",
            "Members with their fines, loans and card.",
            (
                Filter("status", "Status", "select", (("active", "Active"), ("blocked", "Blocked"))),
                Filter("owes", "Fines", "select", (("yes", "Owes something"), ("over", "Over the fine limit"))),
                Filter("has_overdue", "Overdue loans only", "bool"),
                Filter("q", "Search", "text", placeholder="name or number"),
            ),
            (
                Column("member_no", "Member", "mono"),
                Column("name", "Name"),
                Column("status", "Status", "badge"),
                Column("fines_cents", "Fines", "money"),
                Column("open_loans", "Loans", "number"),
                Column("overdue_loans", "Overdue", "number"),
                Column("card", "Card", "mono"),
            ),
            members,
        ),
        Report(
            "circulation",
            "Circulation",
            "Every check-out, renewal and check-in attempt, refusals included.",
            (
                *DATES,
                Filter("action", "Action", "select", ACTIONS),
                Filter("outcome", "Outcome", "select", OUTCOMES),
                Filter("operator", "Operator", "text", placeholder="username"),
                Filter("member_no", "Member", "text", placeholder="M0001"),
                Filter("q", "Item", "text", placeholder="accession or title"),
            ),
            (
                Column("ts", "When", "datetime"),
                Column("action", "Action"),
                Column("outcome", "Outcome", "badge"),
                Column("accession_no", "Item"),
                Column("title", "Title"),
                Column("member_no", "Member", "mono"),
                Column("actor", "Operator"),
                Column("message", "Message"),
            ),
            circulation,
        ),
        Report(
            "operators",
            "Operators",
            "What each staff member did in the period.",
            (*DATES, Filter("operator", "Operator", "text", placeholder="username")),
            (
                Column("name", "Name"),
                Column("username", "User", "mono"),
                Column("role", "Role"),
                Column("checkouts", "Check-outs", "number"),
                Column("renewals", "Renewals", "number"),
                Column("checkins", "Check-ins", "number"),
                Column("refused", "Refused", "number"),
                Column("tags_linked", "Tags linked", "number"),
                Column("inventory_sessions", "Inventories", "number"),
                Column("payments", "Payments", "number"),
                Column("waivers", "Waivers", "number"),
                Column("total", "Total", "number"),
            ),
            operators,
        ),
        Report(
            "rfid_clients",
            "RFID clients",
            "Readers, handhelds, gates, cameras and card printers: state and activity.",
            (*DATES, Filter("kind", "Kind", "select", DEVICE_KINDS)),
            (
                Column("device", "Device", "mono"),
                Column("kind", "Kind"),
                Column("driver", "Driver"),
                Column("status", "Status", "badge"),
                Column("activity", "Activity", "number"),
                Column("unit", ""),
                Column("detail", "Detail"),
                Column("last_activity", "Last activity", "datetime"),
            ),
            rfid_clients,
        ),
        Report(
            "fines",
            "Fines",
            "The fine ledger: charges, payments and waivers.",
            (
                *DATES,
                Filter("kind", "Kind", "select", _money_choices()),
                Filter("member_no", "Member", "text", placeholder="M0001"),
                Filter("q", "Name", "text", placeholder="member name"),
            ),
            (
                Column("ts", "When", "datetime"),
                Column("member_no", "Member", "mono"),
                Column("name", "Name"),
                Column("kind", "Kind", "badge"),
                Column("amount", "Amount", "money"),
                Column("note", "Note"),
                Column("actor", "By"),
            ),
            fines,
        ),
        Report(
            "gate_events",
            "Gate events",
            "Everything the gate saw, with alarms, footfall and message status.",
            (
                *DATES,
                Filter("kind", "Kind", "select", tuple(KIND_LABEL.items())),
                Filter("gate", "Gate", "text", placeholder="gate-1"),
                Filter("alarms_only", "Alarms only", "bool"),
                Filter("q", "Search", "text", placeholder="accession, title or tag"),
            ),
            (
                Column("ts", "When", "datetime"),
                Column("gate", "Gate", "mono"),
                Column("kind", "Result"),
                Column("alarm", "Alarm", "badge"),
                Column("accession_no", "Item"),
                Column("title", "Title"),
                Column("tag_uid", "Tag", "mono"),
                Column("people", "People", "number"),
                Column("messages", "Messages"),
            ),
            gate_events,
        ),
    )
}
