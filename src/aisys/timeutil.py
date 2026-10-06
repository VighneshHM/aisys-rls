"""Time helpers. SQLite returns stored datetimes without a timezone, but we always store UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def aware(dt: datetime) -> datetime:
    """The same moment with an explicit UTC timezone (naive values read back from SQLite are UTC)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit offset, so a browser shows the right local time (a bare timestamp is read as local)."""
    return aware(dt).isoformat() if dt else None
