"""NFR04: the first administrator of a real (non-demo) install is created from the command line."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shelfsense import cli
from shelfsense.app import create_app
from shelfsense.db import make_engine, make_sessionmaker
from shelfsense.models import AuditEvent, User

PASSWORD = "a-long-site-password"  # noqa: S105 - test value


def _run(settings, monkeypatch, *argv, password=PASSWORD):
    monkeypatch.setenv("SHELFSENSE_ADMIN_PASSWORD", password)
    monkeypatch.setattr(cli, "load_settings", lambda _path=None: settings)
    return cli.main(["create-admin", *argv])


@pytest.mark.req("NFR04")
def test_the_first_administrator_can_sign_in_and_is_audited(settings, monkeypatch, capsys):
    assert _run(settings, monkeypatch, "--username", "site.admin", "--name", "Site Admin") == 0
    out = capsys.readouterr()
    assert "created administrator" in out.out
    assert PASSWORD not in out.out + out.err
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/auth/login", json={"username": "site.admin", "password": PASSWORD})
        assert r.status_code == 200, r.text
        who = c.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
        assert who["role"] == "admin"
        assert c.post("/api/auth/login", json={"username": "site.admin", "password": PASSWORD + "x"}).status_code == 401
    with make_sessionmaker(make_engine(settings.database_url))() as db:
        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "admin.bootstrap")).all()
        assert [e.entity_id for e in events] == ["site.admin"]
        assert PASSWORD not in repr([e.detail for e in events])


@pytest.mark.req("NFR04")
@pytest.mark.parametrize(
    ("username", "password", "why"),
    [
        ("site.admin", "short", "characters"),
        ("site.admin", " " * 20, "characters"),
        ("site.admin", "x" * 300, "characters"),
        ("site.admin", cli.DEMO_PASSWORD, "different password"),
        ("site.admin", "site.admin", "characters"),
        ("site.admin\n", PASSWORD, "username must be"),
        ("Site.Admin", PASSWORD, "username must be"),
        ("system", PASSWORD, "username must be"),
        ("ab", PASSWORD, "username must be"),
        ("bad name!", PASSWORD, "username must be"),
        ("../etc", PASSWORD, "username must be"),
    ],
)
def test_weak_passwords_and_bad_names_are_refused(settings, monkeypatch, capsys, username, password, why):
    assert _run(settings, monkeypatch, "--username", username, password=password) == 1
    assert why in capsys.readouterr().err
    with make_sessionmaker(make_engine(settings.database_url))() as db:
        assert db.scalars(select(User)).all() == []


@pytest.mark.req("NFR04")
def test_a_taken_username_is_refused_and_the_first_account_is_unchanged(settings, monkeypatch, capsys):
    assert _run(settings, monkeypatch, "--username", "site.admin") == 0
    assert _run(settings, monkeypatch, "--username", "site.admin", password="another-long-password") == 1
    assert "already taken" in capsys.readouterr().err
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/auth/login", json={"username": "site.admin", "password": PASSWORD}).status_code == 200


@pytest.mark.req("NFR04")
def test_it_will_not_prompt_without_a_terminal_or_the_variable(settings, monkeypatch, capsys):
    monkeypatch.delenv("SHELFSENSE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["create-admin", "--username", "site.admin"]) == 1
    assert "SHELFSENSE_ADMIN_PASSWORD" in capsys.readouterr().err


@pytest.mark.req("NFR04")
def test_control_characters_are_removed_from_the_shown_name(settings, monkeypatch):
    assert _run(settings, monkeypatch, "--username", "site.admin", "--name", "Ada\x1b[31m Admin\r\n") == 0
    with make_sessionmaker(make_engine(settings.database_url))() as db:
        assert db.scalar(select(User.display_name)) == "Ada[31m Admin"
