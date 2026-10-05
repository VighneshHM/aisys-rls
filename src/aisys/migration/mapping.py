"""Column mapping: canonical field -> source header. Auto-suggested from aliases, overridable by the user."""

from __future__ import annotations

import difflib
import re

# Canonical fields, in display order.
FIELDS = [
    "accession_no", "barcode", "title", "author", "isbn", "publisher", "pub_year", "edition", "language",
    "call_number", "location", "item_type", "status", "price", "acquired_on",
]  # fmt: skip
REQUIRED = ("accession_no", "title")

ALIASES: dict[str, list[str]] = {
    "accession_no": [
        "accession no", "accession number", "accession", "acc no", "accno", "acc number", "copy id", "item id",
    ],
    "barcode": ["barcode", "bar code", "item barcode", "barcode no"],
    "title": ["title", "book title", "title proper", "name", "book name", "title of book"],
    "author": ["author", "authors", "main author", "creator", "writer", "author name", "author s"],
    "isbn": ["isbn", "isbn13", "isbn 13", "isbn10", "isbn 10", "isbn no", "isbn number"],
    "publisher": ["publisher", "publisher name", "published by", "imprint"],
    "pub_year": ["year", "pub year", "publication year", "year of publication", "published", "year published"],
    "edition": ["edition", "edn", "ed"],
    "language": ["language", "lang"],
    "call_number": ["call no", "call number", "callno", "classification", "class no", "dewey", "ddc", "shelf mark"],
    "location": ["location", "shelf", "branch", "library", "section", "home location", "shelf location"],
    "item_type": ["type", "item type", "material type", "format", "category", "material"],
    "status": ["status", "item status", "availability"],
    "price": ["price", "cost", "amount", "list price"],
    "acquired_on": [
        "acquisition date", "date acquired", "acquired", "acquired on", "purchase date", "date of acquisition",
        "accession date", "date added",
    ],
}  # fmt: skip


def normalize_header(h: str) -> str:
    """Lower-case, drop bracketed qualifiers such as '(INR)' and punctuation: 'Price (INR)' -> 'price'."""
    h = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", h.casefold())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", h)).strip()


def suggest_mapping(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return (mapping, unmapped_source_headers). Exact alias hits win; then close fuzzy matches."""
    norm = {h: normalize_header(h) for h in headers}
    alias_lookup = {a: f for f, aliases in ALIASES.items() for a in aliases}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for h, n in norm.items():  # pass 1: exact (after normalisation)
        field = alias_lookup.get(n) or (n if n in FIELDS else alias_lookup.get(n.replace(" ", "_")))
        if field and field not in mapping:
            mapping[field] = h
            used.add(h)
    for h, n in norm.items():  # pass 2: fuzzy for what is left
        if h in used or not n:
            continue
        close = difflib.get_close_matches(n, list(alias_lookup), n=1, cutoff=0.86)
        if close and alias_lookup[close[0]] not in mapping:
            mapping[alias_lookup[close[0]]] = h
            used.add(h)
    return mapping, [h for h in headers if h not in used]


def check_mapping(mapping: dict[str, str], headers: list[str]) -> list[str]:
    """Problems that prevent validation (empty list == OK)."""
    problems = []
    for field, header in mapping.items():
        if field not in FIELDS:
            problems.append(f"Unknown target field: {field}")
        elif header not in headers:
            problems.append(f"Column '{header}' (for {field}) is not in the file")
    for field in REQUIRED:
        if field not in mapping:
            problems.append(f"Required field '{field}' is not mapped to any column")
    return problems
