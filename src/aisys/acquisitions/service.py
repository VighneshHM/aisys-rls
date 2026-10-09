"""Acquisition: order books from a supplier and receive them into the catalogue.

An order moves draft -> ordered -> part_received -> received (or cancelled while nothing has arrived). Lines can be
edited only while the order is a draft. Receiving a line creates the bibliographic record (reused when the same book is
already in the catalogue) and one copy per book received, through the same validation as hand entry, with the next
free accession number ("ACQ000001"). The new copies have no RFID tag yet: tagging is the next step.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aisys import audit
from aisys.catalogue import service as catalogue
from aisys.migration import validation
from aisys.models import Item, PoLine, PurchaseOrder, utcnow
from aisys.timeutil import iso

MAX_QUANTITY = 1000
MAX_LINES = 200
STATUS_AFTER_RECEIPT = {True: "received", False: "part_received"}


class AcquisitionError(Exception):
    def __init__(self, status: int, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.status, self.message, self.issues = status, message, issues or []


def line_out(line: PoLine) -> dict:
    return {
        "id": line.id,
        "title": line.title,
        "author": line.author,
        "isbn": line.isbn,
        "publisher": line.publisher,
        "pub_year": line.pub_year,
        "quantity": line.quantity,
        "unit_price_cents": line.unit_price_cents,
        "location": line.location,
        "received": line.received,
        "outstanding": line.quantity - line.received,
        "bib_id": line.bib_id,
    }


def order_out(order: PurchaseOrder, *, with_lines: bool = True) -> dict:
    total = sum(line.quantity * line.unit_price_cents for line in order.lines)
    out = {
        "id": order.id,
        "po_no": order.po_no,
        "supplier": order.supplier,
        "status": order.status,
        "note": order.note,
        "created_by": order.created_by,
        "created_at": iso(order.created_at),
        "ordered_at": iso(order.ordered_at),
        "closed_at": iso(order.closed_at),
        "lines_count": len(order.lines),
        "ordered": sum(line.quantity for line in order.lines),
        "received": sum(line.received for line in order.lines),
        "total_cents": total,
    }
    if with_lines:
        out["lines"] = [line_out(line) for line in order.lines]
    return out


def _clean_line(raw: dict) -> dict:
    """Check one order line with the catalogue's own rules (ISBN, year, text lengths)."""
    title = " ".join(str(raw.get("title") or "").split())
    problems: list[dict] = []
    if not title or len(title) > 500:
        problems.append({"field": "title", "message": "A title of 1 to 500 characters is required"})
    qty = raw.get("quantity", 1)
    if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_QUANTITY:
        problems.append({"field": "quantity", "message": f"Quantity must be a whole number from 1 to {MAX_QUANTITY}"})
    price = raw.get("unit_price_cents", 0)
    if not isinstance(price, int) or isinstance(price, bool) or not 0 <= price <= 10_000_000:
        problems.append({"field": "unit_price_cents", "message": "Price must be 0 to 100,000.00"})
    isbn = validation.normalize_isbn(str(raw.get("isbn") or ""))
    if isbn and not validation.isbn_valid(isbn):
        problems.append({"field": "isbn", "message": "That ISBN is not valid"})
    year = raw.get("pub_year")
    if year is not None and (not isinstance(year, int) or not 1400 <= year <= date.today().year + 1):
        problems.append({"field": "pub_year", "message": "Year of publication looks wrong"})
    location = " ".join(str(raw.get("location") or "").split())
    if len(location) > 100:
        problems.append({"field": "location", "message": "Location is at most 100 characters"})
    if problems:
        raise AcquisitionError(422, "The order line has problems; nothing was saved", problems)
    return {
        "title": title,
        "author": " ".join(str(raw.get("author") or "").split())[:300],
        "isbn": isbn or None,
        "publisher": " ".join(str(raw.get("publisher") or "").split())[:300],
        "pub_year": year,
        "quantity": qty,
        "unit_price_cents": price,
        "location": location,
    }


def next_po_no(db: Session) -> str:
    year = datetime.now(UTC).year
    prefix = f"PO-{year}-"
    numbers = [
        int(n[len(prefix) :]) for (n,) in db.execute(select(PurchaseOrder.po_no)) if re.fullmatch(rf"{prefix}\d+", n)
    ]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def get_order(db: Session, order_id: int) -> PurchaseOrder:
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise AcquisitionError(404, "No such order")
    return order


def list_orders(db: Session, status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    q = select(PurchaseOrder)
    if status:
        q = q.where(PurchaseOrder.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(PurchaseOrder.id.desc()).limit(min(max(limit, 1), 200)).offset(max(offset, 0)))
    return {"total": total, "orders": [order_out(o, with_lines=False) for o in rows]}


def create_order(
    db: Session, supplier: str, note: str, lines: list[dict], *, actor: str, ip: str = ""
) -> PurchaseOrder:
    supplier = " ".join((supplier or "").split())
    if not supplier or len(supplier) > 200:
        raise AcquisitionError(422, "A supplier name of 1 to 200 characters is required")
    if len(lines) > MAX_LINES:
        raise AcquisitionError(422, f"An order has at most {MAX_LINES} lines")
    cleaned = [_clean_line(line) for line in lines]
    for attempt in range(3):  # two people creating an order at once may pick the same number: take the next
        order = PurchaseOrder(
            po_no=next_po_no(db), supplier=supplier, note=(note or "").strip()[:300], created_by=actor
        )
        order.lines = [PoLine(**c) for c in cleaned]
        db.add(order)
        try:
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise AcquisitionError(409, "Could not allocate an order number; try again") from None
    audit.record(
        db, actor=actor, action="acquisition.order.create", entity="order", entity_id=order.po_no,
        detail={"supplier": supplier, "lines": len(cleaned)}, ip=ip,
    )  # fmt: skip
    db.commit()
    return order


def _draft(order: PurchaseOrder) -> None:
    if order.status != "draft":
        raise AcquisitionError(
            409, f"Order {order.po_no} is {order.status.replace('_', ' ')}; only a draft can be edited"
        )


def add_line(db: Session, order_id: int, raw: dict, *, actor: str, ip: str = "") -> PurchaseOrder:
    order = get_order(db, order_id)
    _draft(order)
    if len(order.lines) >= MAX_LINES:
        raise AcquisitionError(422, f"An order has at most {MAX_LINES} lines")
    order.lines.append(PoLine(**_clean_line(raw)))
    audit.record(db, actor=actor, action="acquisition.line.add", entity="order", entity_id=order.po_no, ip=ip)
    db.commit()
    return order


def remove_line(db: Session, order_id: int, line_id: int, *, actor: str, ip: str = "") -> PurchaseOrder:
    order = get_order(db, order_id)
    _draft(order)
    line = next((x for x in order.lines if x.id == line_id), None)
    if line is None:
        raise AcquisitionError(404, "No such line on this order")
    order.lines.remove(line)
    audit.record(db, actor=actor, action="acquisition.line.remove", entity="order", entity_id=order.po_no, ip=ip)
    db.commit()
    return order


def place(db: Session, order_id: int, *, actor: str, ip: str = "") -> PurchaseOrder:
    order = get_order(db, order_id)
    _draft(order)
    if not order.lines:
        raise AcquisitionError(409, "An order with no lines cannot be placed")
    order.status, order.ordered_at = "ordered", utcnow()
    audit.record(db, actor=actor, action="acquisition.order.place", entity="order", entity_id=order.po_no, ip=ip)
    db.commit()
    return order


def cancel(db: Session, order_id: int, *, actor: str, ip: str = "") -> PurchaseOrder:
    order = get_order(db, order_id)
    if order.status not in ("draft", "ordered") or any(line.received for line in order.lines):
        raise AcquisitionError(409, f"Order {order.po_no} cannot be cancelled: books have arrived or it is closed")
    order.status, order.closed_at = "cancelled", utcnow()
    audit.record(db, actor=actor, action="acquisition.order.cancel", entity="order", entity_id=order.po_no, ip=ip)
    db.commit()
    return order


def next_accession(db: Session) -> str:
    numbers = [
        int(a[3:])
        for (a,) in db.execute(select(Item.accession_no).where(Item.accession_no.like("ACQ%")))
        if re.fullmatch(r"ACQ\d+", a)
    ]
    return f"ACQ{max(numbers, default=0) + 1:06d}"


def receive(db: Session, order_id: int, receipts: list[dict], *, actor: str, ip: str = "") -> dict:
    """Book in what arrived. `receipts` is [{line_id, quantity}]. Everything is checked before anything is created."""
    order = get_order(db, order_id)
    if order.status not in ("ordered", "part_received"):
        raise AcquisitionError(
            409, f"Order {order.po_no} is {order.status.replace('_', ' ')}; nothing can be received on it"
        )
    if not receipts:
        raise AcquisitionError(422, "Say which lines arrived")
    by_id = {line.id: line for line in order.lines}
    plan: list[tuple[PoLine, int]] = []
    seen: set[int] = set()
    for r in receipts:
        line, qty = by_id.get(r.get("line_id")), r.get("quantity")
        if line is None or r.get("line_id") in seen:
            raise AcquisitionError(422, "A line is unknown or listed twice")
        seen.add(line.id)
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= line.quantity - line.received:
            raise AcquisitionError(422, f"'{line.title}': only {line.quantity - line.received} still to come")
        plan.append((line, qty))
    created: list[dict] = []
    try:
        for line, qty in plan:
            for _ in range(qty):
                item = catalogue.create_item(
                    db,
                    {
                        "title": line.title,
                        "author": line.author,
                        "isbn": line.isbn or "",
                        "publisher": line.publisher,
                        "pub_year": str(line.pub_year or ""),
                        "accession_no": next_accession(db),
                        "location": line.location,
                        "price": f"{line.unit_price_cents / 100:.2f}",
                        "acquired_on": date.today().isoformat(),
                    },
                    actor=actor,
                    ip=ip,
                    commit=False,
                )  # fmt: skip
                line.bib_id = item.bib_id
                line.received += 1
                created.append(
                    {"item_id": item.id, "accession_no": item.accession_no, "title": line.title, "line_id": line.id}
                )
    except catalogue.CatalogueError as exc:
        db.rollback()
        raise AcquisitionError(422, f"Receiving stopped and nothing was booked in: {exc.message}", exc.issues) from exc
    done = all(line.received >= line.quantity for line in order.lines)
    order.status = STATUS_AFTER_RECEIPT[done]
    if done:
        order.closed_at = utcnow()
    audit.record(
        db, actor=actor, action="acquisition.receive", entity="order", entity_id=order.po_no,
        detail={"items": len(created), "status": order.status}, ip=ip,
    )  # fmt: skip
    db.commit()
    return {"order": order_out(order), "created": created}
