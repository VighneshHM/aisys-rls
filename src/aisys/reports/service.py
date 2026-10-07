"""Running reports, exporting them as CSV, and the small charts on the dashboard."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aisys.circulation import service as circ_service
from aisys.circulation.connector import CirculationConfig
from aisys.models import CircEvent, GateEvent, Loan
from aisys.reports.core import Context, Report, ReportError, per_day
from aisys.reports.definitions import REPORTS

MAX_EXPORT_ROWS = 50_000
KNOWN_PARAMS = {"from", "to"}


def catalogue() -> list[dict]:
    """What the page needs to draw the picker and each report's filter form."""
    return [
        {
            "id": r.id,
            "title": r.title,
            "description": r.description,
            "filters": [
                {
                    "key": f.key,
                    "label": f.label,
                    "kind": f.kind,
                    "options": [{"value": v, "label": lab} for v, lab in f.options],
                    "placeholder": f.placeholder,
                }
                for f in r.filters
            ],
            "columns": [{"key": c.key, "label": c.label, "kind": c.kind} for c in r.columns],
        }
        for r in REPORTS.values()
    ]


def get(report_id: str) -> Report:
    report = REPORTS.get(report_id)
    if report is None:
        raise ReportError(404, f"No report called '{report_id}'")
    return report


def clean_params(report: Report, raw: dict) -> dict:
    """Only the filters this report defines; blank values are dropped. Anything else in the URL is ignored."""
    allowed = {f.key for f in report.filters}
    return {k: v.strip() for k, v in raw.items() if k in allowed and isinstance(v, str) and v.strip()}


def run(ctx: Context, report_id: str, raw_params: dict, limit: int = 25, offset: int = 0) -> dict:
    report = get(report_id)
    params = clean_params(report, raw_params)
    result = report.run(ctx, params, limit, offset)
    return {
        "id": report.id,
        "title": report.title,
        "params": params,
        "total": result.total,
        "limit": limit,
        "offset": offset,
        "rows": result.rows,
        "summary": result.summary,
        "chart": result.chart,
    }


def _cell(kind: str, value) -> str:
    if value is None:
        return ""
    if kind == "money":
        return f"{value / 100:.2f}"
    text = str(value)
    # a spreadsheet reads a cell starting with = + - @ as a formula: neutralise it (data such as titles can start so)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(report: Report, rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([c.label or c.key for c in report.columns])
    for row in rows:
        writer.writerow([_cell(c.kind, row.get(c.key)) for c in report.columns])
    return out.getvalue()


def export(ctx: Context, report_id: str, raw_params: dict) -> tuple[Report, str, int]:
    report = get(report_id)
    params = clean_params(report, raw_params)
    result = report.run(ctx, params, MAX_EXPORT_ROWS, 0)
    return report, to_csv(report, result.rows), result.total


# ------------------------------------------------------------------ the dashboard's charts
def dashboard_charts(db: Session, config: CirculationConfig, days: int = 14) -> dict:
    """Transactions and alarms per day for the last `days` days, and the most overdue loans."""
    days = min(max(days, 2), 60)
    now = datetime.now(UTC)
    lo = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=days - 1)
    hi = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    tx = dict(
        db.execute(
            select(func.date(CircEvent.ts), func.count())
            .where(CircEvent.ts >= lo, CircEvent.ts < hi, CircEvent.outcome == "ok")
            .group_by(func.date(CircEvent.ts))
        ).all()
    )
    alarms = dict(
        db.execute(
            select(func.date(GateEvent.ts), func.count())
            .where(GateEvent.ts >= lo, GateEvent.ts < hi, GateEvent.alarm, GateEvent.duplicate_of.is_(None))
            .group_by(func.date(GateEvent.ts))
        ).all()
    )
    overdue = (
        db.scalars(select(Loan).where(Loan.returned_at.is_(None), Loan.due_at < now).order_by(Loan.due_at).limit(5))
        .unique()
        .all()
    )
    return {
        "days": days,
        "circulation": {"title": "Completed transactions per day", "unit": "number", "points": per_day(tx, lo, hi)},
        "alarms": {"title": "Gate alarms per day", "unit": "number", "points": per_day(alarms, lo, hi)},
        "overdue": [circ_service.loan_out(x, now, config) for x in overdue],
    }
