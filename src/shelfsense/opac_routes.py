"""The public catalogue search (OPAC): what a library visitor may see, with no sign-in.

It shows only what a visitor needs: title, author, publisher, year, ISBN, and for each copy its shelf, call number and
availability. It never shows accession numbers, barcodes, tags, members, loans or due dates. A small per-address limit
stops one visitor from hammering the catalogue. It can be switched off with `[opac] enabled = false`.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from shelfsense.catalogue import service as catalogue
from shelfsense.config import Settings
from shelfsense.deps import client_ip, get_db, get_settings
from shelfsense.shelves import service as shelves

router = APIRouter(prefix="/api/opac", tags=["opac"])

LIMIT_PER_MINUTE = 60
MIN_QUERY = 2
MAX_QUERY = 100
MAX_COPIES = 5000  # one search reads at most this many copies; beyond it the answer says it is incomplete
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()

AVAILABILITY = {
    "available": "Available",
    "on_loan": "On loan",
    "damaged": "Unavailable (being repaired)",
}


def _guard(request: Request, settings: Settings) -> None:
    if not settings.opac_enabled:
        raise HTTPException(404, "Not found")
    now, ip = time.monotonic(), client_ip(request) or "unknown"
    with _lock:
        window = _hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= LIMIT_PER_MINUTE:
            raise HTTPException(
                429,
                detail={"message": "Too many searches; please wait a minute.", "problems": []},
                headers={"Retry-After": "30"},
            )
        window.append(now)
        if len(_hits) > 5000:  # keep the table small on a long-running server
            for key in [k for k, w in _hits.items() if not w or now - w[-1] > 60]:
                _hits.pop(key, None)


def _availability(item) -> str:
    if item.is_reference and item.status == "available":
        return "Reference: use in the library"
    return AVAILABILITY.get(item.status, "Unavailable")


@router.get("/search")
def search(
    request: Request,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _guard(request, settings)
    q = " ".join(q.split())
    if len(q) < MIN_QUERY:
        raise HTTPException(422, detail={"message": f"Type at least {MIN_QUERY} characters to search.", "problems": []})
    if len(q) > MAX_QUERY:  # D-075: a long query was told it was too short
        raise HTTPException(422, detail={"message": f"Type at most {MAX_QUERY} characters to search.", "problems": []})
    limit, offset = min(max(limit, 1), 50), max(offset, 0)
    # every matching copy, not only the first 200, so the count and the paging cover the whole catalogue (D-074);
    # withdrawn and lost copies are left out by the query itself and never use up a page
    rows: list = []
    total_copies = None
    while total_copies is None or len(rows) < min(total_copies, MAX_COPIES):
        total_copies, page, _ = catalogue.search(
            db, q, hide_statuses=shelves.HIDDEN_STATUSES, limit=min(200, MAX_COPIES - len(rows)), offset=len(rows)
        )
        if not page:
            break
        rows.extend(page)
    books: dict[int, dict] = {}
    for item, _tag in rows:
        b = item.bib
        entry = books.setdefault(
            b.id,
            {
                "title": b.title,
                "author": b.author,
                "publisher": b.publisher,
                "year": b.pub_year,
                "isbn": b.isbn,
                "copies": [],
            },
        )
        entry["copies"].append(
            {"shelf": item.location, "call_number": item.call_number, "availability": _availability(item)}
        )
    ordered = list(books.values())
    for entry in ordered:
        entry["copies"].sort(
            key=lambda c: (c["availability"] != "Available", c["shelf"], shelves.natural_key(c["call_number"]))
        )
        entry["available"] = sum(1 for c in entry["copies"] if c["availability"] == "Available")
    complete = len(rows) >= (total_copies or 0)
    return {"query": q, "total": len(ordered), "complete": complete, "results": ordered[offset : offset + limit]}


@router.get("/shelf")
def shelf(
    request: Request,
    code: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """A shelf as a row of books (title, call number, availability only) so a visitor can see where to look."""
    _guard(request, settings)
    try:
        v = shelves.view(db, code)
    except shelves.ShelfError as exc:
        raise HTTPException(exc.status, detail={"message": exc.message, "problems": []}) from exc
    return {
        "code": v["shelf"]["code"],
        "books": [
            {
                "title": b["title"],
                "author": b["author"],
                "call_number": b["call_number"],
                "availability": AVAILABILITY.get(b["status"], "Unavailable")
                if not b["is_reference"]
                else "Reference: use in the library",
            }
            for b in v["books"]
        ],
    }
