"""Time helpers. SQLite returns stored datetimes without a timezone, but we always store UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime


def aware(dt: datetime) -> datetime:
    """The same moment with an explicit UTC timezone (naive values read back from SQLite are UTC)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def library_today() -> date:
    """Today as the library counts days: in UTC (assumption A-20), whatever the server's own time zone is (D-060)."""
    return datetime.now(UTC).date()


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit offset, so a browser shows the right local time (a bare timestamp is read as local)."""
    return aware(dt).isoformat() if dt else None
