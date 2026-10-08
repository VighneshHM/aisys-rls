"""Application factory."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from aisys import __version__, migrate, pages
from aisys.auth.permissions import sync_rbac
from aisys.backup_routes import router as backup_router
from aisys.catalogue_routes import router as catalogue_router
from aisys.circulation.registry import build_connector
from aisys.circulation_routes import router as circulation_router
from aisys.config import Settings, load_settings
from aisys.db import make_engine, make_sessionmaker
from aisys.gate_routes import notify_router
from aisys.gate_routes import router as gate_router
from aisys.integrations.credentials import CredentialProvider, MockCredentialProvider
from aisys.inventory_routes import router as inventory_router
from aisys.logging_setup import configure_logging
from aisys.migration_routes import router as migration_router
from aisys.notifications.providers import build_providers
from aisys.reports_routes import router as reports_router
from aisys.rfid.registry import DeviceRegistry
from aisys.rfid_routes import router as rfid_router
from aisys.routes import router

log = logging.getLogger("aisys")


class RevalidatingStaticFiles(StaticFiles):
    """Static files the browser must re-check on every use (cheap: an unchanged file answers 304 via its ETag)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings | None = None,
    credential_provider: CredentialProvider | None = None,
    *,
    run_migrations: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_format)

    if run_migrations:
        migrate.upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    factory = make_sessionmaker(engine)
    with factory() as db:
        sync_rbac(db)

    app = FastAPI(title=settings.app_name, version=__version__)
    app.state.settings = settings
    app.state.maintenance = threading.Event()  # set while a restore runs: everything else waits (see below)
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.credential_provider = credential_provider or MockCredentialProvider()
    app.state.devices = DeviceRegistry(settings.devices)  # fails fast on a bad [[devices]] config
    app.state.ilms = build_connector(settings.circulation)  # fails fast on a bad [ilms] config
    app.state.providers = build_providers(settings.notifications)  # and on a bad [notifications] config
    app.include_router(router)
    app.include_router(migration_router)
    app.include_router(catalogue_router)
    app.include_router(rfid_router)
    app.include_router(circulation_router)
    app.include_router(inventory_router)
    app.include_router(gate_router)
    app.include_router(notify_router)
    app.include_router(reports_router)
    app.include_router(backup_router)

    @app.middleware("http")
    async def maintenance_gate(request: Request, call_next):
        """While a restore replaces the data, nobody else may read or write it: a change made in the middle would be
        lost or half-applied. Everyone else gets a clear, temporary 503 (and the restore finishes in seconds)."""
        if app.state.maintenance.is_set() and not request.url.path.startswith("/static"):
            return JSONResponse(
                {
                    "detail": {
                        "message": "The library data is being restored from a backup. Please try again shortly.",
                        "problems": [],
                    }
                },
                status_code=503,
                headers={"Retry-After": "5"},
            )
        return await call_next(request)

    app.mount(
        "/static", RevalidatingStaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static"
    )
    app.include_router(pages.router)

    log.info("application ready", extra={"ctx": {"version": __version__, "env": settings.environment}})
    return app
