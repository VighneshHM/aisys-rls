from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aisys.app import create_app
from aisys.cli import DEMO_PASSWORD, seed_demo
from aisys.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        log_level="WARNING",
        max_failed_logins=3,
        lockout_minutes=15,
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)  # runs real Alembic migrations
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
