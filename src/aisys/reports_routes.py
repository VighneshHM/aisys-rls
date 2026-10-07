"""HTTP API for reports: the catalogue, running a report with filters, CSV export, and the dashboard's charts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from aisys.circulation.connector import CirculationConfig
from aisys.config import Settings
from aisys.deps import Principal, get_db, get_settings, require
from aisys.reports import service
from aisys.reports.core import Context, ReportError
from aisys.rfid.registry import DeviceRegistry

router = APIRouter(prefix="/api/reports", tags=["reports"])
CanView = Depends(require("reports.view"))


def get_config(settings: Settings = Depends(get_settings)) -> CirculationConfig:
    return settings.circulation


def get_context(
    request: Request, db: Session = Depends(get_db), config: CirculationConfig = Depends(get_config)
) -> Context:
    devices: DeviceRegistry = request.app.state.devices
    return Context(db, config, devices)


def _http(exc: ReportError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "problems": []})


@router.get("")
def catalogue(_: Principal = CanView):
    return {"reports": service.catalogue()}


@router.get("/charts")
def charts(
    days: int = 14,
    _: Principal = CanView,
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(get_config),
):
    return service.dashboard_charts(db, config, days)


@router.get("/{report_id}")
def run_report(
    report_id: str,
    request: Request,
    limit: int = 25,
    offset: int = 0,
    _: Principal = CanView,
    ctx: Context = Depends(get_context),
):
    """Run one report. Its filters are ordinary query parameters (see `GET /api/reports` for each report's list)."""
    try:
        return service.run(ctx, report_id, dict(request.query_params), min(max(limit, 1), 200), max(offset, 0))
    except ReportError as exc:
        raise _http(exc) from exc


@router.get("/{report_id}/export.csv")
def export_report(report_id: str, request: Request, _: Principal = CanView, ctx: Context = Depends(get_context)):
    """The same report and filters as a CSV file, every row (up to 50,000)."""
    try:
        report, body, total = service.export(ctx, report_id, dict(request.query_params))
    except ReportError as exc:
        raise _http(exc) from exc
    return Response(
        body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{report.id}.csv"',
            "X-Total-Rows": str(total),
            "Cache-Control": "private, no-store",
        },
    )
