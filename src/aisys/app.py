"""Application factory."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from aisys import __version__, migrate, pages, settings_admin
from aisys.acquisitions_routes import router as acquisitions_router
from aisys.acquisitions_routes import serials as serials_router
from aisys.admin_routes import router as admin_router
from aisys.auth.permissions import sync_rbac
from aisys.backup_routes import router as backup_router
from aisys.catalogue_routes import router as catalogue_router
from aisys.circulation.registry import build_connector
from aisys.circulation_routes import router as circulation_router
from aisys.config import Settings, load_settings
from aisys.db import make_engine, make_sessionmaker
from aisys.gate import service as gate_service
from aisys.gate_routes import notify_router
from aisys.gate_routes import router as gate_router
from aisys.integrations.credentials import CredentialProvider, MockCredentialProvider
from aisys.integrations.netcat import build_source
from aisys.inventory_routes import router as inventory_router
from aisys.labels_routes import router as labels_router
from aisys.lifecycle_routes import router as updates_router
from aisys.logging_setup import configure_logging
from aisys.members_routes import router as members_router
from aisys.migration_routes import router as migration_router
from aisys.models import AppSetting
from aisys.notifications import runner
from aisys.notifications.providers import build_providers
from aisys.opac_routes import router as opac_router
from aisys.reports_routes import router as reports_router
from aisys.rfid.registry import DeviceRegistry
from aisys.rfid_routes import router as rfid_router
from aisys.routes import router
from aisys.settings_routes import router as settings_router
from aisys.shelves_routes import router as shelves_router

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
        saved = settings_admin.overrides(db.scalars(select(AppSetting)))
    base_settings = settings  # what the TOML file and environment say
    settings = settings_admin.apply(settings, saved)  # then the rules changed on the Settings page

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Run the notification loop (if `auto_dispatch_seconds` is set) and the gate loop (if `[gate] poll_seconds`
        is set) for as long as the application runs."""
        loop = gate_loop = None
        if settings.notifications.auto_dispatch_s > 0:
            loop = runner.Loop(
                lambda: runner.run_once(
                    app.state.session_factory,
                    app.state.providers,
                    app.state.settings.notifications,
                    library_name=app.state.settings.app_name,
                ),
                settings.notifications.auto_dispatch_s,
                paused=app.state.maintenance.is_set,
            ).start()
        if settings.gate.poll_seconds > 0:

            def gate_pass() -> dict:
                with app.state.session_factory() as db:
                    return gate_service.poll_all(db, app.state.devices, app.state.settings.gate)

            gate_loop = runner.Loop(
                gate_pass, settings.gate.poll_seconds, paused=app.state.maintenance.is_set, name="aisys-gate"
            ).start()
        app.state.notify_loop, app.state.gate_loop = loop, gate_loop
        try:
            yield
        finally:
            for running in (loop, gate_loop):
                if running is not None:
                    running.stop()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.base_settings = base_settings
    app.state.maintenance = threading.Event()  # set while a restore runs: everything else waits (see below)
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.credential_provider = credential_provider or MockCredentialProvider()
    app.state.devices = DeviceRegistry(settings.devices)  # fails fast on a bad [[devices]] config
    app.state.ilms = build_connector(settings.circulation)  # fails fast on a bad [ilms] config
    app.state.netcat = build_source(settings.netcat_driver)  # fails fast on a bad [netcat] config
    app.state.providers = build_providers(settings.notifications)  # and on a bad [notifications] config
    app.include_router(router)
    app.include_router(migration_router)
    app.include_router(catalogue_router)
    app.include_router(rfid_router)
    app.include_router(circulation_router)
    app.include_router(members_router)
    app.include_router(inventory_router)
    app.include_router(gate_router)
    app.include_router(notify_router)
    app.include_router(reports_router)
    app.include_router(backup_router)
    app.include_router(admin_router)
    app.include_router(settings_router)
    app.include_router(opac_router)
    app.include_router(acquisitions_router)
    app.include_router(serials_router)
    app.include_router(labels_router)
    app.include_router(shelves_router)
    app.include_router(updates_router)

    def too_large(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": {"message": "A number in the request is too large.", "problems": []}}, status_code=422
        )

    app.add_exception_handler(OverflowError, too_large)  # e.g. offset=99999999999999999999 or an id that big

    @app.exception_handler(StatementError)
    async def statement_error(request: Request, exc: StatementError):
        if isinstance(exc.orig, OverflowError):  # SQLite cannot hold the number: the caller's mistake, not ours
            return too_large(request, exc)
        raise exc

    @app.middleware("http")
    async def no_nul_in_json(request: Request, call_next):
        """A NUL character is never meaningful text: stored, it breaks CSV exports and the tools that read them (D-079).
        Refused once, here, for every JSON body, rather than in each of the many text fields."""
        if request.method in ("POST", "PUT", "PATCH") and "json" in request.headers.get("content-type", ""):
            body = await request.body()
            if b"\x00" in body or b"\\u0000" in body.lower():
                return JSONResponse(
                    {"detail": {"message": "Text may not contain a NUL character (\\u0000)", "problems": []}},
                    status_code=422,
                )
        return await call_next(request)

    kiosk_hosts = frozenset(settings.opac_kiosk_hosts)

    @app.middleware("http")
    async def kiosk_host(request: Request, call_next):
        """A public terminal reaches the system by a kiosk host name ([opac] kiosk_hosts). There, only the public
        catalogue exists: every staff page and API answers "not found", no sign-in is possible, and any session cookie
        or token the browser sends is dropped before the request is handled, so a staff session can never be used
        or shown there. Staff use the normal host name, so their sessions are never touched by a visitor's page."""
        # registered first, so the security-header and maintenance middlewares wrap its own answers too (D-064);
        # a trailing dot ("opac.library.local.") names the same host and must not slip past the check
        if not kiosk_hosts or (request.url.hostname or "").lower().rstrip(".") not in kiosk_hosts:
            return await call_next(request)
        path = request.url.path
        if path == "/":
            return RedirectResponse("/opac", status_code=303)
        if not (path == "/opac" or path.startswith(("/api/opac/", "/static/")) or path in ("/health", "/health/ready")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        request.scope["headers"] = [
            (k, v) for k, v in request.scope["headers"] if k not in (b"cookie", b"authorization")
        ]
        response = await call_next(request)
        if "set-cookie" in response.headers:
            del response.headers["set-cookie"]
        return response

    @app.middleware("http")
    async def safe_headers(request: Request, call_next):
        """Headers every response carries: no content sniffing, no framing, no referrer leaks; HSTS once on HTTPS."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if response.headers.get("content-type", "").startswith("text/html"):
            # a page must never be shown again from the browser's memory (Back button) after the person signed out
            response.headers.setdefault("Cache-Control", "no-store")
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        if secure or settings.tls_certfile:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

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
