"""FR11: the Settings page and API - see the set-up, change the library's rules, undo, audit, and survive a restart."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from aisys.app import create_app
from aisys.config import Settings
from conftest import login_card
from test_circulation import book, out

pytestmark = pytest.mark.req("FR11")

URL = "/api/admin/settings"


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def get(client, hdr):
    r = client.get(URL, headers=hdr)
    assert r.status_code == 200, r.text
    return {i["key"]: i for i in r.json()["items"]}


def put(client, hdr, **values):
    return client.put(URL, headers=hdr, json={"values": {k.replace("__", "."): v for k, v in values.items()}})


def audit_actions(client, hdr, prefix):
    r = client.get("/api/audit/events", headers=hdr, params={"action": prefix, "limit": 50})
    assert r.status_code == 200, r.text
    return r.json()["events"]


# ------------------------------------------------------------------ who may see and change it
def test_only_administrators_may_use_settings(client, admin, lib, clerk):
    assert client.get(URL, headers=admin).status_code == 200
    for hdr in (lib, clerk):
        assert client.get(URL, headers=hdr).status_code == 403
        assert put(client, hdr, circulation__loan_days=7).status_code == 403
        assert client.delete(URL + "/circulation.loan_days", headers=hdr).status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == 401
    assert put(client, {}, circulation__loan_days=7).status_code == 401


def test_the_page_renders_for_the_administrator_only(client, admin, lib):
    client.cookies.set("aisys_session", admin["Authorization"].split()[1])
    page = client.get("/settings")
    assert page.status_code == 200 and "Library rules" in page.text and "Settings" in page.text
    client.cookies.set("aisys_session", lib["Authorization"].split()[1])
    assert client.get("/settings").status_code == 403
    client.cookies.clear()
    assert client.get("/settings", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_the_page_is_in_the_menu_for_admins_only(client, admin, lib):
    client.cookies.set("aisys_session", admin["Authorization"].split()[1])
    assert 'href="/settings"' in client.get("/audit").text
    client.cookies.set("aisys_session", lib["Authorization"].split()[1])
    assert 'href="/settings"' not in client.get("/circulation").text


# ------------------------------------------------------------------ reading
def test_every_rule_is_listed_with_its_limits_and_file_value(client, admin):
    items = get(client, admin)
    assert {"circulation.loan_days", "circulation.fine_per_day_cents", "notifications.remind_days_before",
            "security.lockout_minutes", "opac.enabled", "gate.duplicate_window_s"} <= set(items)  # fmt: skip
    d = items["circulation.loan_days"]
    assert (d["min"], d["max"], d["unit"], d["kind"]) == (1, 365, "days", "int")
    assert d["value"] == d["file_value"] == 14 and d["changed"] is False
    assert items["opac.enabled"]["kind"] == "bool" and items["opac.enabled"]["value"] is True


def test_the_read_only_part_never_shows_a_secret(client, admin):
    body = client.get(URL, headers=admin).json()
    text = " ".join(f"{f['label']} {f['value']}" for f in body["readonly"])
    assert "staff-1" in text and "sip2" in text.lower()
    s: Settings = client.app.state.settings
    for secret in (s.secret_key, "password", "token"):
        assert not secret or secret not in text.lower()
    assert s.database_url not in text  # only the file name, never the whole path
    assert "test.db" in text


# ------------------------------------------------------------------ changing
def test_a_change_applies_at_once_and_is_audited(client, admin, lib):
    r = put(client, admin, circulation__loan_days=7)
    assert r.status_code == 200 and r.json()["changed"] == ["circulation.loan_days"]
    item = get(client, admin)["circulation.loan_days"]
    assert item["value"] == 7 and item["file_value"] == 14 and item["changed"] and item["updated_by"] == "admin"
    book(client, lib, "S1")
    due = datetime.datetime.fromisoformat(out(client, lib, "M0001", "S1").json()["loan"]["due_at"])
    days = (due - datetime.datetime.now(datetime.UTC)).total_seconds() / 86400
    assert 6.9 < days < 7.1  # the new rule is what the next loan uses, with no restart
    ev = audit_actions(client, admin, "admin.settings.update")
    assert len(ev) == 1 and ev[0]["actor"] == "admin"
    assert ev[0]["detail"]["changes"]["circulation.loan_days"] == {"from": 14, "to": 7}


def test_the_fine_limit_and_renewals_follow_the_new_rules(client, admin, lib):
    assert put(client, admin, circulation__max_loans=1).status_code == 200
    book(client, lib, "L1")
    book(client, lib, "L2")
    assert out(client, lib, "M0001", "L1").status_code == 200
    r = out(client, lib, "M0001", "L2")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "loan_limit"
    assert put(client, admin, circulation__max_loans=5).status_code == 200
    assert out(client, lib, "M0001", "L2").status_code == 200


def test_several_values_are_saved_together_and_a_repeat_changes_nothing(client, admin):
    r = put(client, admin, circulation__grace_days=2, notifications__reminders=False, gate__duplicate_window_s=30)
    assert sorted(r.json()["changed"]) == [
        "circulation.grace_days",
        "gate.duplicate_window_s",
        "notifications.reminders",
    ]
    s = client.app.state.settings
    assert s.circulation.grace_days == 2 and s.notifications.reminders is False and s.gate.duplicate_window_s == 30
    again = put(client, admin, circulation__grace_days=2)
    assert again.json() == {"changed": [], "message": "Nothing changed."}
    assert len(audit_actions(client, admin, "admin.settings.update")) == 1  # the repeat left no second entry


def test_saving_the_value_it_already_has_does_not_create_an_override(client, admin):
    assert put(client, admin, circulation__loan_days=14).json()["changed"] == []
    assert get(client, admin)["circulation.loan_days"]["changed"] is False


@pytest.mark.parametrize(
    "key, value",
    [
        ("circulation__loan_days", 0),
        ("circulation__loan_days", 366),
        ("circulation__loan_days", -5),
        ("circulation__loan_days", "abc"),
        ("circulation__loan_days", ""),
        ("circulation__loan_days", 3.5),
        ("circulation__loan_days", "1_0"),
        ("circulation__loan_days", "٣٤"),
        ("circulation__loan_days", "0x10"),
        ("circulation__loan_days", [7]),
        ("circulation__loan_days", None),
        ("circulation__loan_days", True),
        ("circulation__loan_days", "99999999999999999999999"),
        ("security__session_ttl_minutes", 1),
        ("notifications__remind_days_before", 31),
        ("notifications__reminders", "maybe"),
        ("opac__enabled", 7),
    ],
)
def test_a_bad_value_is_refused_in_plain_words_and_saves_nothing(client, admin, key, value):
    r = put(client, admin, **{key: value})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message"].startswith("Nothing was saved") and detail["problems"][0]["message"]
    assert all(not i["changed"] for i in get(client, admin).values())
    assert audit_actions(client, admin, "admin.settings") == []


def test_one_bad_value_saves_none_of_the_batch(client, admin):
    r = put(client, admin, circulation__loan_days=7, circulation__max_loans=0)
    assert r.status_code == 422
    assert client.app.state.settings.circulation.loan_days == 14
    assert not get(client, admin)["circulation.loan_days"]["changed"]


def test_only_listed_rules_can_be_changed(client, admin):
    for key in ("database_url", "secret_key", "circulation.ilms_endpoint", "devices", "../x", ""):
        r = client.put(URL, headers=admin, json={"values": {key: 1}})
        assert r.status_code == 422, key
    assert client.put(URL, headers=admin, json={"values": {}}).status_code == 422
    assert client.put(URL, headers=admin, json={}).status_code == 422
    assert client.put(URL, headers=admin, json={"values": [1]}).status_code == 422
    assert client.put(URL, headers=admin, content=b"not json").status_code == 422


# ------------------------------------------------------------------ undoing
def test_use_file_value_goes_back_and_is_audited(client, admin):
    put(client, admin, circulation__loan_days=7)
    r = client.delete(URL + "/circulation.loan_days", headers=admin)
    assert r.status_code == 200 and r.json()["value"] == 14
    assert get(client, admin)["circulation.loan_days"]["changed"] is False
    assert client.app.state.settings.circulation.loan_days == 14
    assert [e["detail"]["from"] for e in audit_actions(client, admin, "admin.settings.reset")] == [7]


def test_resetting_what_was_never_changed_or_does_not_exist(client, admin):
    assert client.delete(URL + "/circulation.loan_days", headers=admin).status_code == 409
    assert client.delete(URL + "/nothing.here", headers=admin).status_code == 404
    assert client.delete(URL + "/database_url", headers=admin).status_code == 404


# ------------------------------------------------------------------ the toggles that other pages obey
def test_the_public_catalogue_can_be_switched_off_and_on(client, admin):
    assert client.get("/api/opac/search", params={"q": "atlas"}).status_code == 200
    put(client, admin, opac__enabled=False)
    assert client.get("/api/opac/search", params={"q": "atlas"}).status_code == 404
    put(client, admin, opac__enabled="true")
    assert client.get("/api/opac/search", params={"q": "atlas"}).status_code == 200


def test_sign_in_limits_follow_the_settings(client, admin):
    put(client, admin, security__max_failed_logins=1)
    client.cookies.clear()
    bad = client.post("/api/auth/login", json={"username": "clerk", "password": "wrong-password-1"})
    assert bad.status_code == 401
    again = client.post("/api/auth/login", json={"username": "clerk", "password": "wrong-password-1"})
    assert again.status_code in (401, 423, 429)
    assert client.app.state.settings.max_failed_logins == 1


# ------------------------------------------------------------------ restart and bad rows
def test_saved_rules_survive_a_restart_and_the_file_still_has_its_say(client, admin, settings):
    put(client, admin, circulation__loan_days=21, gate__duplicate_window_s=45)
    with TestClient(create_app(settings)) as again:  # a new process reading the same database
        s = again.app.state.settings
        assert s.circulation.loan_days == 21 and s.gate.duplicate_window_s == 45
        assert again.app.state.base_settings.circulation.loan_days == 14  # the file value is still known
        hdr = login_card(again, "STAFFADMIN01")
        assert get(again, hdr)["circulation.loan_days"]["file_value"] == 14


def test_a_bad_saved_row_is_ignored_and_never_stops_the_system_starting(client, admin, settings):
    import sqlite3

    path = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as con:
        con.execute("insert into app_settings values ('circulation.loan_days','banana','x','2026-01-01 00:00:00')")
        con.execute("insert into app_settings values ('old.removed.key','5','x','2026-01-01 00:00:00')")
        con.execute("insert into app_settings values ('circulation.max_loans','99999','x','2026-01-01 00:00:00')")
    with TestClient(create_app(settings)) as again:
        c = again.app.state.settings.circulation
        assert c.loan_days == 14 and c.max_loans == 5  # the defaults, not the junk


def test_the_loop_reads_the_current_reminder_rules(client, admin):
    put(client, admin, notifications__remind_days_before=3, notifications__max_attempts=7)
    n = client.app.state.settings.notifications
    assert n.remind_days_before == 3 and n.max_attempts == 7


def test_not_a_number_in_the_body_is_refused_not_a_server_error(client, admin):
    for literal in ("NaN", "Infinity", "-Infinity"):
        r = client.put(
            URL, headers={**admin, "Content-Type": "application/json"},
            content=('{"values":{"circulation.loan_days":' + literal + "}}").encode(),
        )  # fmt: skip
        assert r.status_code == 422, (literal, r.status_code)
    assert client.app.state.settings.circulation.loan_days == 14


def test_a_whole_number_written_as_a_float_or_with_spaces_is_accepted(client, admin):
    assert put(client, admin, circulation__loan_days=7.0).json()["changed"] == ["circulation.loan_days"]
    assert put(client, admin, circulation__max_loans=" 9 ").json()["changed"] == ["circulation.max_loans"]
    assert client.app.state.settings.circulation.max_loans == 9
