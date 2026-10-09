from __future__ import annotations

import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from aisys import migrate
from aisys.app import create_app
from aisys.circulation.connector import CirculationConfig
from aisys.cli import DEMO_PASSWORD, seed_demo
from aisys.config import Settings
from aisys.notifications.providers import NotificationConfig


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory):
    """An empty database at the newest schema, built once per test process.

    Running every migration for every test is slow on a CI disk (fsync-heavy), so each test copies this file instead."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    migrate.upgrade(f"sqlite:///{path.as_posix()}")
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return path


@pytest.fixture
def settings(tmp_path, migrated_db) -> Settings:
    shutil.copyfile(migrated_db, tmp_path / "test.db")  # create_app then finds nothing left to migrate
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        log_level="WARNING",
        backup_dir=str(tmp_path / "backups"),  # never the real data/backups folder
        max_failed_logins=3,
        lockout_minutes=15,
        circulation=CirculationConfig(ilms_backoff_s=0.0),  # retries without waiting
        notifications=NotificationConfig(backoff_s=0.0),
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)  # the copied database is already at head; migrations are tested in their own files
    seed_demo(settings)
    with TestClient(app) as c:
        c.settings = settings  # type: ignore[attr-defined]
        yield c


def login_card(client: TestClient, uid: str) -> dict:
    r = client.post("/api/auth/card-login", json={"uid": uid})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin(client):
    return login_card(client, "STAFFADMIN01")


@pytest.fixture
def clerk(client):
    return login_card(client, "STAFFCLERK01")


@pytest.fixture
def admin_password() -> str:
    return DEMO_PASSWORD
