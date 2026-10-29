"""Foundation tests: AC07 (card login + RBAC), NFR02 (security), NFR06/07 (migrations, health)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shelfsense import migrate
from shelfsense.app import create_app
from shelfsense.db import make_engine
from shelfsense.logging_setup import mask


# ---------- AC07: staff smart-card login + RBAC ----------
@pytest.mark.req("AC07", "FR11")
def test_staff_card_logs_in_and_gets_role_permissions(client):
    r = client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "librarian"
    assert "circulation.checkout" in body["user"]["permissions"]
    assert "admin.users" not in body["user"]["permissions"]


@pytest.mark.req("AC07")
def test_patron_card_cannot_log_in(client):
    r = client.post("/api/auth/card-login", json={"uid": "PATRON000001"})
    assert r.status_code == 403
    assert "Patron" in r.json()["detail"]
    assert "token" not in r.json()


@pytest.mark.req("AC07", "NFR02")
def test_patron_login_attempt_is_audited(client, settings):
    client.post("/api/auth/card-login", json={"uid": "PATRON000001"})
    admin_token = client.post("/api/auth/card-login", json={"uid": "STAFFADMIN01"}).json()["token"]
    # admin has no audit.read; auditor does
    auditor = client.post("/api/auth/card-login", json={"uid": "STAFFAUDIT01"}).json()["token"]
    assert admin_token
    rows = client.get(
        "/api/audit?action=auth.login.card&outcome=denied", headers={"Authorization": f"Bearer {auditor}"}
    ).json()
    assert any(r["detail"].get("reason") == "patron_card_login_attempt" for r in rows)


@pytest.mark.req("AC07")
def test_unknown_and_malformed_cards_rejected(client):
    assert client.post("/api/auth/card-login", json={"uid": "NOSUCHCARD1"}).status_code == 401
    assert client.post("/api/auth/card-login", json={"uid": "bad uid!"}).status_code == 400


@pytest.mark.req("AC07", "NFR02")
def test_disabled_staff_card_rejected(client, settings):
    engine = make_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE cards SET active=0 WHERE uid='STAFFCLERK01'"))
    assert client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"}).status_code == 403


@pytest.mark.req("AC07", "FR04")
def test_rbac_enforced_and_denial_audited(client, clerk, admin):
    assert client.get("/api/admin/users", headers=clerk).status_code == 403
    assert client.get("/api/admin/users", headers=admin).status_code == 200
    auditor = client.post("/api/auth/card-login", json={"uid": "STAFFAUDIT01"}).json()["token"]
    rows = client.get("/api/audit?action=authz.denied", headers={"Authorization": f"Bearer {auditor}"}).json()
    assert rows and rows[0]["actor"] == "clerk"


@pytest.mark.req("AC07")
def test_unauthenticated_requests_get_401(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/admin/users").status_code == 401


@pytest.mark.req("AC07")
def test_logout_revokes_session(client, admin):
    assert client.get("/api/auth/me", headers=admin).status_code == 200
    assert client.post("/api/auth/logout", headers=admin).status_code == 200
    assert client.get("/api/auth/me", headers=admin).status_code == 401


@pytest.mark.req("AC07", "FR11")
def test_issue_cards_and_users(client, admin):
    r = client.post(
        "/api/admin/users", headers=admin, json={"username": "newbie", "display_name": "New B", "role": "librarian"}
    )
    assert r.status_code == 201
    assert (
        client.post(
            "/api/admin/cards", headers=admin, json={"uid": "STAFFNEW001", "kind": "staff", "username": "newbie"}
        ).status_code
        == 201
    )
    assert client.post("/api/auth/card-login", json={"uid": "STAFFNEW001"}).status_code == 200
    # patron card must carry a member, staff card must carry a user
    assert (
        client.post("/api/admin/cards", headers=admin, json={"uid": "PATRONNEW01", "kind": "patron"}).status_code == 422
    )
    assert (
        client.post("/api/admin/cards", headers=admin, json={"uid": "STAFFNEW002", "kind": "staff"}).status_code == 422
    )
    assert (
        client.post(
            "/api/admin/cards", headers=admin, json={"uid": "STAFFNEW001", "kind": "staff", "username": "newbie"}
        ).status_code
        == 409
    )


# ---------- NFR02: password login hardening ----------
@pytest.mark.req("NFR02")
def test_password_login_and_lockout(client, admin_password):
    assert client.post("/api/auth/login", json={"username": "admin", "password": admin_password}).status_code == 200
    for _ in range(3):
        assert (
            client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
        )
    assert client.post("/api/auth/login", json={"username": "admin", "password": admin_password}).status_code == 423


@pytest.mark.req("NFR02")
def test_unknown_user_and_bad_password_look_identical(client, admin_password):
    a = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever-pass"})
    b = client.post("/api/auth/login", json={"username": "admin", "password": "whatever-pass"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


@pytest.mark.req("NFR02")
def test_input_validation_rejects_bad_payloads(client, admin):
    assert (
        client.post(
            "/api/admin/users", headers=admin, json={"username": "X", "display_name": "x", "role": "admin"}
        ).status_code
        == 422
    )
    assert client.post("/api/auth/card-login", json={}).status_code == 422


@pytest.mark.req("NFR02")
def test_passwords_and_tokens_not_stored_in_clear(client, settings, admin_password):
    token = client.post("/api/auth/login", json={"username": "admin", "password": admin_password}).json()["token"]
    con = sqlite3.connect(settings.database_url.removeprefix("sqlite:///"))
    dump = "\n".join(str(r) for t in ("users", "sessions") for r in con.execute(f"SELECT * FROM {t}"))
    assert admin_password not in dump and token not in dump


@pytest.mark.req("NFR02", "NFR07")
def test_log_masking():
    assert "hunter2" not in mask('{"password": "hunter2"}')
    assert "1234567890123456" not in mask("card 1234567890123456")


# ---------- audit log ----------
@pytest.mark.req("NFR02", "FR11")
def test_audit_log_is_append_only(client, settings):
    engine = make_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_events(ts,actor,action,detail) VALUES (CURRENT_TIMESTAMP,'t','x','{}')"))
    for sql in ("UPDATE audit_events SET actor='evil'", "DELETE FROM audit_events"):
        with pytest.raises(Exception, match="append-only"), engine.begin() as conn:
            conn.execute(text(sql))


# ---------- NFR06/07: migrations, health, version ----------
@pytest.mark.req("NFR06")
def test_migrations_upgrade_and_downgrade(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    migrate.upgrade(url)
    engine = make_engine(url)
    assert migrate.current_revision(engine) == migrate.head_revision()
    migrate.downgrade(url, "base")
    assert migrate.current_revision(engine) is None
    migrate.upgrade(url)  # idempotent re-apply
    assert migrate.current_revision(engine) == migrate.head_revision()


@pytest.mark.req("NFR07")
def test_health_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["schema"]["ok"] is True


@pytest.mark.req("FR11", "NFR07")
def test_system_info_reports_versions(client, admin, clerk):
    info = client.get("/api/system/info", headers=admin).json()
    assert info["version"] and info["schema_revision"] == info["schema_head"]
    assert client.get("/api/system/info", headers=clerk).status_code == 403


@pytest.mark.req("FR11")
def test_web_pages(client):
    assert "Staff sign-in" in client.get("/").text
    assert client.get("/dashboard", follow_redirects=False).status_code in (302, 307)
    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})  # sets cookie
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Leo Librarian" in page.text


@pytest.mark.req("NFR06", "FR12")
def test_migrate_creates_missing_data_directory(tmp_path):
    """Regression: a fresh install has no data/ folder yet."""
    url = f"sqlite:///{(tmp_path / 'fresh' / 'nested' / 'a.db').as_posix()}"
    migrate.upgrade(url)
    assert migrate.current_revision(make_engine(url)) == migrate.head_revision()


@pytest.mark.req("FR02", "NFR03")
def test_ui_assets_served_locally_no_external_requests(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    for page in (client.get("/").text, css.text):
        assert "http://" not in page.replace("http://www.w3.org", "") and "cdn" not in page.lower()


@pytest.mark.req("FR11", "AC07")
def test_navigation_only_shows_what_role_may_use(client):
    client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"})
    html = client.get("/dashboard").text
    assert "Circulation" in html and "Catalogue" in html
    assert "Administration" not in html and "Audit log" not in html  # clerk lacks these permissions
    assert "{%" not in html and "{{" not in html  # templates fully rendered


@pytest.mark.req("FR11", "NFR02")
def test_demo_cards_hidden_in_production(settings):
    from dataclasses import replace

    prod = replace(settings, environment="production", secret_key="x" * 40)
    with TestClient(create_app(prod)) as c:
        assert "STAFFADMIN01" not in c.get("/").text
