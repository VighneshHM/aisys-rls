# ruff: noqa: F811  (fixtures are imported from test_lifecycle and then used by name, which ruff reads as a redefinition)
"""AC09: the Updates page and its API (read-only)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aisys import __version__
from aisys.app import create_app
from conftest import login_card
from test_lifecycle import (  # noqa: F401 - fixtures
    _keyfile,
    fake_release,
    installed,
    make_package,
    new_pkg,
    no_smoke,
    vendor,
)

pytestmark = pytest.mark.req("AC09", "FR12")


@pytest.fixture
def managed(installed, settings, monkeypatch):
    monkeypatch.setenv("AISYS_HOME", str(installed.path))
    with TestClient(create_app(settings)) as c:
        from aisys.cli import seed_demo

        seed_demo(settings)
        c.root = installed  # type: ignore[attr-defined]
        yield c


def test_not_an_installed_copy_says_so(client, admin, monkeypatch):
    monkeypatch.delenv("AISYS_HOME", raising=False)
    s = client.get("/api/updates/status", headers=admin).json()
    assert s["managed"] is False and s["running_version"] == __version__
    r = client.post("/api/updates/check", json={"name": "x.zip"}, headers=admin)
    assert r.status_code == 409
    client.cookies.set("aisys_session", admin["Authorization"].split()[1])
    assert client.get("/updates").status_code == 200


def test_status_of_an_installed_copy(managed):
    admin = login_card(managed, "STAFFADMIN01")
    s = managed.get("/api/updates/status", headers=admin).json()
    assert s["managed"] and s["current"] == "0.1.0" and s["previous"] is None
    assert s["licence"]["status"] == "valid" and s["history"][0]["action"] == "install"
    assert "running" not in s


def test_only_administrators_may_see_it(managed):
    clerk = login_card(managed, "STAFFCLERK01")
    assert managed.get("/api/updates/status", headers=clerk).status_code == 403
    assert managed.post("/api/updates/check", json={"name": "a.zip"}, headers=clerk).status_code == 403
    managed.cookies.clear()  # the card logins above left a session cookie in this client
    assert managed.get("/api/updates/status").status_code == 401


def test_page_renders_for_admin_and_is_forbidden_for_clerk(managed):
    admin, clerk = login_card(managed, "STAFFADMIN01"), login_card(managed, "STAFFCLERK01")
    managed.cookies.set("aisys_session", admin["Authorization"].split()[1])
    assert managed.get("/updates").status_code == 200
    managed.cookies.set("aisys_session", clerk["Authorization"].split()[1])
    assert managed.get("/updates").status_code == 403


def test_check_a_waiting_package_changes_nothing(managed, tmp_path, vendor):
    admin = login_card(managed, "STAFFADMIN01")
    pkg = new_pkg(tmp_path, vendor)
    import shutil

    shutil.copytree(pkg, managed.root.incoming / pkg.name)
    s = managed.get("/api/updates/status", headers=admin).json()
    assert pkg.name in s["incoming"]
    r = managed.post("/api/updates/check", json={"name": pkg.name}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "0.1.0" and r.json()["to"] == "0.2.0" and r.json()["schema_to"] == "0009"
    assert managed.get("/api/updates/status", headers=admin).json()["current"] == "0.1.0"
    (managed.root.incoming / pkg.name / "config.sample.toml").write_text("tampered")
    r = managed.post("/api/updates/check", json={"name": pkg.name}, headers=admin)
    assert r.status_code == 422 and "altered" in r.json()["detail"]["message"]


@pytest.mark.parametrize("name", ["../../etc/passwd", "..", "a/b", "nope.zip", "", "x" * 300, "bad name.zip"])
def test_check_only_accepts_plain_names_inside_incoming(managed, name):
    admin = login_card(managed, "STAFFADMIN01")
    r = managed.post("/api/updates/check", json={"name": name}, headers=admin)
    assert r.status_code in (404, 422)
