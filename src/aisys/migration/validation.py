"""Row validation and normalisation (pure functions, no database access).

Severity model: an *error* rejects the row; a *warning* keeps the row but records that a value was
defaulted or dropped. Rules are deliberately explicit so they can be listed in the data dictionary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

ACCESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$")
BARCODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

REQUIRED_LABELS = {"title": "Title", "accession_no": "Accession number"}
MAX_LEN = {"title": 500, "author": 300, "publisher": 300, "edition": 100, "language": 50,
           "call_number": 100, "location": 100}  # fmt: skip

ITEM_TYPE_ALIASES = {
    "book": "book", "books": "book", "monograph": "book", "text": "book", "textbook": "book",
    "reference": "reference", "ref": "reference", "reference book": "reference",
    "serial": "serial", "journal": "serial", "magazine": "serial", "periodical": "serial",
    "dvd": "media", "cd": "media", "media": "media", "audio": "media", "video": "media",
    "other": "other",
}  # fmt: skip
STATUS_ALIASES = {
    "available": "available", "in": "available", "on shelf": "available", "": "available",
    "lost": "lost", "missing": "lost", "withdrawn": "withdrawn", "discarded": "withdrawn",
    "damaged": "damaged", "repair": "damaged",
}  # fmt: skip
LOAN_STATES = {"issued", "on loan", "checked out", "borrowed", "out"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y")


@dataclass
class Issue:
    field: str
    code: str
    message: str
    severity: str = "error"  # error | warning

    def as_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class RowResult:
    clean: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", CONTROL_RE.sub(" ", value or "")).strip()


# ---------------------------------------------------------------- ISBN
def normalize_isbn(raw: str) -> str:
    """Strip an 'ISBN', 'ISBN-13:' ... label and separators; keep digits and a trailing X."""
    text = re.sub(r"^\s*isbn(?:[- ]?1[03])?\s*:?", "", raw or "", flags=re.IGNORECASE)
    return re.sub(r"[\s-]", "", text).upper()


def isbn_valid(isbn: str) -> bool:
    if re.fullmatch(r"\d{9}[\dX]", isbn):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if re.fullmatch(r"\d{13}", isbn):
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn[:12]))
        return (10 - total % 10) % 10 == int(isbn[12])
    return False


# ---------------------------------------------------------------- dates, numbers
def parse_date(value: str) -> date | None:
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()  # noqa: DTZ007 - calendar date, no tz
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def parse_price(value: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d.,-]", "", value)
    if not cleaned or cleaned.count("-") > 1 or ("-" in cleaned and not cleaned.startswith("-")):
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")  # 1,200.50
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[1]) <= 2:
        cleaned = cleaned.replace(",", ".")  # 12,50
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- dedupe key
def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text.casefold())).strip()


def bib_dedupe_key(title: str, author: str, year: int | None, isbn: str | None) -> str:
    if isbn:
        return f"isbn:{isbn}"
    return f"t:{_norm_key(title)[:250]}|{_norm_key(author)[:100]}|{year or ''}"


# ---------------------------------------------------------------- row validation
def validate_row(values: dict[str, str]) -> RowResult:
    """`values` maps canonical field -> raw string (missing fields simply absent)."""
    res = RowResult()
    v = {k: clean_text(values.get(k, "")) for k in values}
    err = lambda f, c, m: res.issues.append(Issue(f, c, m))  # noqa: E731
    warn = lambda f, c, m: res.issues.append(Issue(f, c, m, "warning"))  # noqa: E731
    clean = res.clean

    for f in ("title", "accession_no"):
        if not v.get(f):
            err(f, "required_missing", f"{REQUIRED_LABELS[f]} is required")

    acc = v.get("accession_no", "")
    if acc and not ACCESSION_RE.match(acc):
        err("accession_no", "accession_format", "Accession no. must be 1-32 letters, digits, . _ / -")
    clean["accession_no"] = acc

    bc = v.get("barcode", "")
    if bc and not BARCODE_RE.match(bc):
        err("barcode", "barcode_format", "Barcode must be 1-64 letters, digits, . _ -")
    clean["barcode"] = bc or None

    for f, limit in MAX_LEN.items():
        text = v.get(f, "")
        if len(text) > limit:
            err(f, "too_long", f"{f.replace('_', ' ').capitalize()} exceeds {limit} characters")
        clean[f] = text

    raw_isbn = v.get("isbn", "")
    isbn = normalize_isbn(raw_isbn)
    if isbn and not isbn_valid(isbn):
        err("isbn", "isbn_invalid", f"'{raw_isbn}' is not a valid ISBN-10/13 (length or check digit)")
        isbn = ""
    clean["isbn"] = isbn or None

    year_raw = v.get("pub_year", "")
    clean["pub_year"] = None
    if year_raw:
        m = re.search(r"\b(\d{4})\b", year_raw)
        year = int(m.group(1)) if m else None
        if year is None or not 1000 <= year <= datetime.now(UTC).year + 1:
            warn("pub_year", "year_invalid", f"Publication year '{year_raw}' ignored")
        else:
            clean["pub_year"] = year

    type_raw = v.get("item_type", "").casefold()
    item_type = ITEM_TYPE_ALIASES.get(type_raw, "" if type_raw else "book")
    if not item_type:
        warn("item_type", "item_type_unknown", f"Item type '{v['item_type']}' stored as 'other'")
        item_type = "other"
    clean["item_type"] = item_type
    clean["is_reference"] = item_type == "reference"

    status_raw = v.get("status", "").casefold()
    if status_raw in LOAN_STATES:
        warn("status", "loan_not_migrated", "Item is on loan in the source; loans are not migrated, set to available")
        clean["status"] = "available"
    elif status_raw in STATUS_ALIASES:
        clean["status"] = STATUS_ALIASES[status_raw]
    else:
        warn("status", "status_unknown", f"Status '{v['status']}' set to available")
        clean["status"] = "available"

    price_raw = v.get("price", "")
    clean["price"] = None
    if price_raw:
        price = parse_price(price_raw)
        if price is None or price < 0 or price > Decimal("99999999.99"):
            err("price", "price_invalid", f"'{price_raw}' is not a valid non-negative price")
        else:
            clean["price"] = str(price)

    date_raw = v.get("acquired_on", "")
    clean["acquired_on"] = None
    if date_raw:
        d = parse_date(date_raw)
        if d is None or d > datetime.now(UTC).date():
            err("acquired_on", "date_invalid", f"'{date_raw}' is not a valid past date (day-first formats assumed)")
        else:
            clean["acquired_on"] = d.isoformat()

    clean["dedupe_key"] = bib_dedupe_key(clean["title"], clean["author"], clean["pub_year"], clean["isbn"])
    return res
