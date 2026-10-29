"""Building blocks shared by every report: definitions, filter parsing, date ranges, paging and per-day series."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from shelfsense.circulation.connector import CirculationConfig
from shelfsense.rfid.registry import DeviceRegistry

MAX_DAYS_CHART = 60
NO_MATCH = "%﷐%"  # a pattern no real text contains (U+FDD0 is a Unicode non-character)


class ReportError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Filter:
    key: str
    label: str
    kind: str = "text"  # text | select | date | bool
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for a select
    placeholder: str = ""


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = "text"  # text | number | money | datetime | mono | badge


@dataclass
class Context:
    """What a report may use: the database, the library policy, and the configured devices."""

    db: Session
    config: CirculationConfig
    devices: DeviceRegistry


@dataclass
class Result:
    rows: list[dict]
    total: int
    summary: list[dict] = field(default_factory=list)  # [{label, value, kind}]
    chart: dict | None = None  # {title, unit, points: [{label, value}]}


@dataclass(frozen=True)
class Report:
    id: str
    title: str
    description: str
    filters: tuple[Filter, ...]
    columns: tuple[Column, ...]
    run: Callable[[Context, dict, int, int], Result]  # (ctx, params, limit, offset); limit <= 0 means every row


def parse_date(value: str, label: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReportError(422, f"{label} must be a date like 2026-10-07") from exc


def date_range(params: dict) -> tuple[datetime | None, datetime | None]:
    """[start, end) in UTC from the `from` and `to` dates; `to` counts the whole day."""
    start, end = parse_date(params.get("from", ""), "From"), parse_date(params.get("to", ""), "To")
    if start and end and end < start:
        raise ReportError(422, "'To' is before 'From'")
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC) if start else None
    hi = None
    if end and end < date.max:  # the very last date has no "next day" to stop at: it simply means no upper limit
        hi = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    return lo, hi


def choice(params: dict, key: str, allowed: tuple[tuple[str, str], ...]) -> str:
    value = (params.get(key) or "").strip()
    if value and value not in {v for v, _ in allowed}:
        raise ReportError(422, f"'{value}' is not a valid choice for {key}")
    return value


def flag(params: dict, key: str) -> bool:
    return str(params.get(key, "")).lower() in ("1", "true", "yes", "on")


def text(params: dict, key: str, limit: int = 100) -> str:
    return (params.get(key) or "").strip()[:limit]


def like(value: str) -> str:
    """A LIKE pattern for a typed search. Wildcards are removed, and a search made only of wildcards matches nothing."""
    cleaned = value.lower().replace("%", "").replace("_", "")
    return f"%{cleaned}%" if cleaned else NO_MATCH


def per_day(days_with_counts: dict[str, int], lo: datetime | None, hi: datetime | None) -> list[dict]:
    """Chart points for each day between the first and last day with data (or the chosen range), zeros included."""
    keys = sorted(days_with_counts)
    if not keys and not (lo and hi):
        return []
    first = lo.date() if lo else date.fromisoformat(keys[0])
    last = (hi - timedelta(days=1)).date() if hi else date.fromisoformat(keys[-1])
    if last < first:
        return []
    if (last - first).days >= MAX_DAYS_CHART:
        first = last - timedelta(days=MAX_DAYS_CHART - 1)
    out, day = [], first
    while day <= last:
        out.append({"label": day.isoformat(), "value": days_with_counts.get(day.isoformat(), 0)})
        day += timedelta(days=1)
    return out
