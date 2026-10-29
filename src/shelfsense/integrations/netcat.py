"""Net cataloguing: fetch a book's description from an outside catalogue by ISBN, so staff do not retype it.

The outside service is an adapter behind `BibliographicSource`. The only implementation here is a mock holding a
small synthetic set of records; a real one would call a national library or a shared catalogue and is a class
registered under a driver name, like the other adapters. The lookup fills the add-item form; a person still checks it
and saves it, and it goes through the same validation as anything typed by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


class NetCatalogueError(Exception):
    """The outside catalogue did not answer (down, slow, or refused)."""


@runtime_checkable
class BibliographicSource(Protocol):
    name: str

    def lookup(self, isbn: str) -> dict | None:
        """The record for an ISBN-13 (digits only), or None if the catalogue has no such book."""


def _isbn13(prefix12: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(prefix12))
    return prefix12 + str((10 - total % 10) % 10)


# SYNTHETIC records: invented titles, authors and numbers (the ISBNs have valid check digits, nothing else is real)
_RECORDS = [
    ("978000100001", "The Lighthouse Ledger", "Marit Solheim", "Northlight Press", 2019, "1st", "English"),
    ("978000100002", "Gardens of Salt and Iron", "Dara Okonkwo-Reyes", "Harbour & Finch", 2021, "2nd", "English"),
    ("978000100003", "A Short History of Quiet", "Ilya Brandt", "Meridian Books", 2015, "1st", "English"),
    ("978000100004", "Cartography for Beginners", "Noor Haddad", "Compass House", 2023, "1st", "English"),
    ("978000100005", "The Orchard Clock", "Beatrix Vane", "Little Lantern", 2018, "3rd", "English"),
    ("978000100006", "Practical Beekeeping", "Tomas Lindqvist", "Greenfield Guides", 2020, "4th", "English"),
    ("978000100007", "Letters from the Tidal Flats", "Anneke de Vries", "Salt Marsh Editions", 2017, "1st", "English"),
    ("978000100008", "Mathematics of Everyday Things", "Priyanka Rao", "Orbit Academic", 2022, "2nd", "English"),
    ("978000100009", "El jardin de los relojes", "Lucia Ferrer", "Ediciones Faro", 2016, "1st", "Spanish"),
    ("978000100010", "Introduction to Network Resilience", "Kofi Mensah", "Orbit Academic", 2024, "1st", "English"),
]
SAMPLE = {
    _isbn13(p): {
        "title": t, "author": a, "publisher": pub, "pub_year": y, "edition": e, "language": lang, "isbn": _isbn13(p),
    }
    for p, t, a, pub, y, e, lang in _RECORDS
}  # fmt: skip


class MockNetCatalogue:
    """Answers from the built-in synthetic set. `fail_next` makes the next lookup time out, to test the error path."""

    name = "mock"

    def __init__(self, **_options: object) -> None:
        self._fail = 0

    def fail_next(self, times: int = 1) -> None:
        self._fail += times

    def lookup(self, isbn: str) -> dict | None:
        if self._fail:
            self._fail -= 1
            raise NetCatalogueError("The outside catalogue did not answer in time")
        found = SAMPLE.get(isbn)
        return dict(found, source=self.name) if found else None


_DRIVERS: dict[str, Callable[..., BibliographicSource]] = {"mock": MockNetCatalogue}


def register_source(name: str, factory: Callable[..., BibliographicSource]) -> None:
    _DRIVERS[name] = factory


def build_source(driver: str) -> BibliographicSource:
    try:
        return _DRIVERS[driver]()
    except KeyError:
        raise ValueError(f"Unknown net-cataloguing driver '{driver}' (known: {', '.join(sorted(_DRIVERS))})") from None
