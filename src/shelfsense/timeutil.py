"""Time helpers. SQLite returns stored datetimes without a timezone, but we always store UTC."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

# How far the application's clock is moved from the real one. Always zero, except while `seed-demo` replays weeks of
# library history (loans taken and returned, fines, passages) at the moments they are meant to have happened.
_offset = timedelta(0)


def utcnow() -> datetime:
    """The current moment in UTC: the one clock every record's timestamp comes from."""
    return datetime.now(UTC) + _offset


@contextmanager
def clock_at(moment: datetime) -> Iterator[None]:
    """Run the block as if it were `moment` (demo data only; never used while the application serves requests)."""
    global _offset
    before, _offset = _offset, moment - datetime.now(UTC)
    try:
        yield
    finally:
        _offset = before


def aware(dt: datetime) -> datetime:
    """The same moment with an explicit UTC timezone (naive values read back from SQLite are UTC)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def library_today() -> date:
    """Today as the library counts days: in UTC (assumption A-20), whatever the server's own time zone is (D-060)."""
    return utcnow().date()


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit offset, so a browser shows the right local time (a bare timestamp is read as local)."""
    return aware(dt).isoformat() if dt else None
