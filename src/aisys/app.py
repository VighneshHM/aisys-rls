"""Application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from aisys import __version__, migrate, pages
from aisys.auth.permissions import sync_rbac
from aisys.config import Settings, load_settings
from aisys.db import make_engine, make_sessionmaker
from aisys.integrations.credentials import CredentialProvider, MockCredentialProvider
from aisys.logging_setup import configure_logging
from aisys.routes import router

log = logging.getLogger("aisys")


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
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.credential_provider = credential_provider or MockCredentialProvider()
    app.include_router(router)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
    app.include_router(pages.router)

    log.info("application ready", extra={"ctx": {"version": __version__, "env": settings.environment}})
    return app
