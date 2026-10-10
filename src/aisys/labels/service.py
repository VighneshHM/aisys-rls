"""Spine labels and barcode labels for items, and the data for a patron card face.

A label is plain data (text lines and barcode bar widths); the page draws it as an SVG and the browser prints it, or a
print job (text) is queued for a label printer through the notification queue's print channel.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys.labels import code128
from aisys.models import Item
from aisys.shelves.service import natural_key

MAX_LABELS = 200
SPINE_LINE = 12  # characters across a spine label
SPINE_LINES = 4


class LabelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def spine_lines(call_number: str) -> list[str]:
    """A call number split into the short lines a narrow spine label holds: 'QA 76.9 D3' -> QA / 76.9 / D3."""
    lines: list[str] = []
    for word in (call_number or "").split():
        while len(word) > SPINE_LINE:
            lines.append(word[:SPINE_LINE])
            word = word[SPINE_LINE:]
        lines.append(word)
    return lines[:SPINE_LINES]


def barcode_of(item: Item) -> str:
    """What the barcode carries: the item's barcode if it has one, otherwise its accession number."""
    value = item.barcode or item.accession_no
    return value if code128.encodable(value) else item.accession_no


def label_for(item: Item) -> dict:
    value = barcode_of(item)
    return {
        "id": item.id,
        "accession_no": item.accession_no,
        "title": item.bib.title[:70],
        "author": item.bib.author[:40],
        "call_number": item.call_number,
        "spine": spine_lines(item.call_number),
        "location": item.location,
        "barcode": value,
        "modules": code128.encode(value) if code128.encodable(value) else "",
    }


def pick_items(db: Session, ids: list[int] | None, location: str | None) -> list[Item]:
    if ids:
        if len(ids) > MAX_LABELS:
            raise LabelError(422, f"At most {MAX_LABELS} labels at a time")
        found = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids)))}
        unknown = sorted({i for i in ids if i not in found})
        if unknown:  # D-078: they were dropped without a word, so a job came out short
            raise LabelError(404, f"No item with number {', '.join(map(str, unknown[:10]))}")
        items = [found[i] for i in ids]
    elif location:
        items = list(
            db.scalars(select(Item).where(Item.location == location, Item.status.not_in(("withdrawn", "lost"))))
        )
        if len(items) > MAX_LABELS:
            raise LabelError(
                422, f"That shelf has {len(items)} books; labels are made {MAX_LABELS} at a time (pick them by number)"
            )
    else:
        raise LabelError(422, "Choose a shelf or some items")
    items.sort(key=lambda i: (natural_key(i.call_number), i.accession_no))
    return items


def text_job(labels: list[dict], kind: str) -> tuple[str, str]:
    """(subject, body) of a print job for a label printer: one block of text per label."""
    blocks = []
    for lab in labels:
        if kind == "spine":
            blocks.append("\n".join(lab["spine"] or [lab["accession_no"]]) + f"\n[{lab['accession_no']}]")
        else:
            blocks.append(f"{lab['barcode']}\n{lab['title']}")
    return f"{len(labels)} {kind} label(s)", "\n\n----\n\n".join(blocks)
