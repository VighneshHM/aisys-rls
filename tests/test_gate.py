"""AC06: gate alarm with accession number, CCTV snapshot and queued notifications (FR07, FR09)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from aisys import migrate
from aisys.cli import main as cli_main
from aisys.gate.service import classify
from aisys.notifications.providers import (
    NotificationConfig,
    ProviderConfigError,
    build_providers,
)
from aisys.rfid.registry import DeviceConfig, DeviceRegistry
from conftest import login_card
from test_circulation import out, sql
from test_tagging import make_item, place, tag


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def admin_h(client):
    return login_card(client, "STAFFADMIN01")


@pytest.fixture
def gate(client):
    return client.app.state.devices.get("gate-1", "gate")


def tagged(client, lib, acc, title="A Book"):
    item = make_item(client, lib, acc, title)
    uid = place(client, lib)
    assert tag(client, lib, item["id"], uid).status_code == 201
    return item, uid


def walk(client, hdr, refs=(), **kw):
    r = client.post("/api/gate/mock/gates/gate-1/walk", headers=hdr, json={"item_refs": list(refs), **kw})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def queue(client, hdr, **q):
    return client.get("/api/notifications", headers=hdr, params={"limit": 100, **q}).json()


# ================================================================== AC06
@pytest.mark.req("AC06", "FR07", "FR09")
def test_an_unauthorised_removal_records_the_accession_takes_a_photo_and_queues_emails(client, lib, gate, admin_h):
    tagged(client, lib, "G1", "Gate Book")
    [ev] = walk(client, lib, ["G1"])  # nobody checked it out, so its security bit is still on
    assert ev["kind"] == "unauthorised" and ev["alarm"] is True and ev["decided_by"] == "security_bit"
    assert ev["accession_no"] == "G1" and ev["title"] == "Gate Book" and ev["has_image"] is True
    assert gate.indications == ["alarm"]  # the siren sounded

    image = client.get(f"/api/gate/events/{ev['id']}/image", headers=lib)
    assert image.status_code == 200 and image.headers["content-type"] == "image/svg+xml"
    assert b"G1" in image.content and b"MOCK CCTV" in image.content and b"Unauthorised removal" in image.content

    detail = client.get(f"/api/gate/events/{ev['id']}", headers=lib).json()
    got = sorted((n["channel"], n["recipient"], n["status"]) for n in detail["notifications"])
    assert got == [  # the demo's alert recipients: admin by email, the librarian by email and print
        ("email", "head-librarian@library.example", "queued"),
        ("email", "security@library.example", "queued"),
        ("print", "front-desk", "queued"),
    ]
    assert "G1" in detail["notifications"][0]["body"] and "Gate Book" in detail["notifications"][0]["body"]
    assert queue(client, lib)["counts"]["queued"] == 3  # queued, not yet sent: delivery is its own step


@pytest.mark.req("AC06", "FR09")
def test_queued_messages_are_delivered_by_channel_with_the_right_words(client, lib, admin_h):
    tagged(client, lib, "G2", "Another Book")
    me = [
        u for u in client.get("/api/notifications/prefs", headers=admin_h).json()["users"] if u["username"] == "admin"
    ][0]
    r = client.put(
        f"/api/notifications/prefs/{me['user_id']}", headers=admin_h,
        json={"channels": {"sms": {"active": True, "address": "+441234567890"}}},
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["channels"]["sms"]["active"] is True
    walk(client, lib, ["G2"])
    assert queue(client, lib)["counts"]["queued"] == 4  # admin: email + sms; librarian: email + print
    done = client.post("/api/notifications/dispatch", headers=admin_h).json()
    assert done["sent"] == 4 and done["failed"] == 0 and done["waiting"] == 0
    box = {m["channel"]: m for m in client.get("/api/notifications/outbox", headers=admin_h).json()["messages"]}
    assert set(box) == {"email", "sms", "print"}
    assert (
        "G2" in box["email"]["body"]
        and "Another Book" in box["email"]["body"]
        and box["email"]["subject"].startswith("ALARM")
    )
    assert len(box["sms"]["body"]) <= 160 and "G2" in box["sms"]["body"]
    assert "SECURITY ALERT" in box["print"]["body"] and box["print"]["to"] == "front-desk"
    assert queue(client, lib)["counts"] == {"queued": 0, "sent": 4, "failed": 0, "cancelled": 0}


# ================================================================== the alarm rules
@pytest.mark.req("FR07")
def test_the_rules_for_what_counts_as_an_alarm():
    class Item:  # only presence matters to `classify`
        pass

    item = Item()
    assert classify(True, item, False) == ("unauthorised", True)
    assert classify(True, item, True) == ("armed_on_loan", True)
    assert classify(False, item, True) == ("authorised", False)
    assert classify(False, item, False) == ("bit_off", False)
    assert classify(True, None, False) == ("unissued", True)
    assert classify(False, None, False) == ("unissued", False)


@pytest.mark.req("FR07")
def test_each_kind_of_passage_through_the_gate(client, lib, gate):
    _, _ = tagged(client, lib, "K1")  # never lent
    _, _ = tagged(client, lib, "K2")  # lent properly: check-out switches the bit off on the reader
    assert out(client, lib, "M0001", "K2").json()["security"]["status"] == "disarmed"
    [authorised] = walk(client, lib, ["K2"])
    assert (
        authorised["kind"] == "authorised" and authorised["alarm"] is False and authorised["loan_member_no"] == "M0001"
    )
    assert authorised["has_image"] is False

    _, uid3 = tagged(client, lib, "K3")
    client.app.state.devices.get("staff-1").remove_tag(uid3)  # not on the reader, so check-out cannot switch it off
    assert out(client, lib, "M0002", "K3").json()["security"]["status"] == "not_on_reader"
    [leak] = walk(client, lib, ["K3"])
    assert leak["kind"] == "armed_on_loan" and leak["alarm"] is True and "switch it off at the desk" in leak["note"]

    [switched] = walk(client, lib, ["K1"], bit="off")  # bit switched off by hand, no loan
    assert switched["kind"] == "bit_off" and switched["alarm"] is False and switched["has_image"] is False
    [stranger] = walk(client, lib, unregistered=1)
    assert stranger["kind"] == "unissued" and stranger["alarm"] is True and stranger["accession_no"] == ""
    assert gate.indications == ["ok", "alarm", "warning", "alarm"]  # what the gate did for each


@pytest.mark.req("FR07", "NFR06")
def test_the_gate_works_while_the_library_system_is_down(client, lib):
    ilms = client.app.state.ilms.inner
    tagged(client, lib, "O1")
    ilms.fail_next("error", times=30)  # the ILMS is completely unavailable
    calls = len(ilms.calls)
    [ev] = walk(client, lib, ["O1"])
    assert ev["alarm"] is True and ev["kind"] == "unauthorised" and ev["decided_by"] == "security_bit"
    assert len(ilms.calls) == calls  # the gate never asked the library system anything


@pytest.mark.req("FR07", "NFR06")
def test_a_repeat_alarm_for_the_same_tag_is_recorded_but_not_re_sent(client, lib, gate):
    tagged(client, lib, "D1")
    [first] = walk(client, lib, ["D1"])
    [again] = walk(client, lib, ["D1"])
    assert again["alarm"] is True and again["duplicate_of"] == first["id"] and again["has_image"] is False
    assert gate.indications == ["alarm", "alarm"]  # the siren still sounds every time
    assert queue(client, lib)["counts"]["queued"] == 3  # but only the first alarm queued messages
    summary = client.get("/api/gate/summary", headers=lib).json()
    assert summary["alarms"] == 1 and summary["unauthorised"] == 1 and summary["passages"] == 2


@pytest.mark.req("FR07")
def test_footfall_counts_people_even_without_tags(client, lib):
    assert walk(client, lib, people=3) == []  # three people, nothing carried: no event
    walk(client, lib, unregistered=1, people=2)  # two people and a stray tag
    summary = client.get("/api/gate/summary", headers=lib).json()
    assert summary["passages"] == 2 and summary["people"] == 5


@pytest.mark.req("AC06", "NFR06")
def test_a_camera_failure_still_records_and_announces_the_alarm(client, lib, admin_h):
    tagged(client, lib, "C1")
    assert client.post("/api/gate/mock/camera/fail", headers=lib).status_code == 200
    [ev] = walk(client, lib, ["C1"])
    assert ev["alarm"] and ev["has_image"] is False and "camera failed" in ev["image_error"]
    assert client.get(f"/api/gate/events/{ev['id']}/image", headers=lib).status_code == 404
    client.post("/api/notifications/dispatch", headers=admin_h)
    body = client.get("/api/notifications/outbox", headers=admin_h).json()["messages"][-1]["body"]
    assert "No CCTV snapshot" in body  # the message says so


@pytest.mark.req("FR07", "NFR02")
def test_the_snapshot_cannot_run_anything_in_the_browser(client, lib):
    tagged(client, lib, "X1", "<script>alert(1)</script>")
    [ev] = walk(client, lib, ["X1"])
    image = client.get(f"/api/gate/events/{ev['id']}/image", headers=lib)
    assert b"<script>" not in image.content and b"&lt;script&gt;" in image.content  # the title is escaped
    assert (
        image.headers["x-content-type-options"] == "nosniff" and "sandbox" in image.headers["content-security-policy"]
    )
    assert image.headers["cache-control"] == "private, no-store"  # never kept by the browser after sign-out


@pytest.mark.req("FR07")
def test_no_recipients_means_no_messages_but_the_alarm_is_still_recorded(client, lib, admin_h):
    for u in client.get("/api/notifications/prefs", headers=admin_h).json()["users"]:
        off = {c: {"active": False, "address": s["address"]} for c, s in u["channels"].items() if s["address"]}
        client.put(f"/api/notifications/prefs/{u['user_id']}", headers=admin_h, json={"channels": off})
    tagged(client, lib, "N1")
    [ev] = walk(client, lib, ["N1"])
    assert ev["alarm"] and "No alert recipients" in ev["note"] and queue(client, lib)["counts"]["queued"] == 0


# ================================================================== delivery: retries
@pytest.mark.req("FR09", "NFR06")
def test_failed_deliveries_are_retried_then_given_up_and_can_be_retried_by_hand(client, lib, admin_h):
    tagged(client, lib, "R1")
    walk(client, lib, ["R1"])
    fail = client.post("/api/notifications/mock/fail", headers=admin_h, json={"channel": "email", "times": 3})
    assert fail.json()["pending"] == 3
    first = client.post("/api/notifications/dispatch", headers=admin_h).json()
    assert first["sent"] == 1 and first["retry_later"] == 2  # the print slip went; both emails wait
    assert {n["attempts"] for n in queue(client, lib, status="queued")["notifications"]} == {1}
    # the mock fails 3 sends in all: one used so far on each... two emails tried once each, so one failure remains
    second = client.post("/api/notifications/dispatch", headers=admin_h).json()
    assert second["sent"] == 1 and second["retry_later"] == 1  # the next send fails, then the provider recovers
    third = client.post("/api/notifications/dispatch", headers=admin_h).json()
    assert third["sent"] == 1 and queue(client, lib)["counts"] == {"queued": 0, "sent": 3, "failed": 0, "cancelled": 0}


@pytest.mark.req("FR09", "NFR06")
def test_a_message_is_marked_failed_after_the_attempt_limit_and_can_be_retried(client, lib, admin_h):
    tagged(client, lib, "R2")
    me = [
        u
        for u in client.get("/api/notifications/prefs", headers=admin_h).json()["users"]
        if u["username"] == "librarian"
    ][0]
    client.put(  # leave exactly one recipient channel: the librarian's print slip
        f"/api/notifications/prefs/{me['user_id']}", headers=admin_h,
        json={"channels": {"email": {"active": False, "address": ""}}},
    )  # fmt: skip
    admin = [
        u for u in client.get("/api/notifications/prefs", headers=admin_h).json()["users"] if u["username"] == "admin"
    ][0]
    client.put(
        f"/api/notifications/prefs/{admin['user_id']}",
        headers=admin_h,
        json={"channels": {"email": {"active": False, "address": ""}}},
    )
    walk(client, lib, ["R2"])
    assert queue(client, lib)["counts"]["queued"] == 1
    client.post(
        "/api/notifications/mock/fail", headers=admin_h, json={"channel": "print", "mode": "timeout", "times": 3}
    )
    for _ in range(3):
        client.post("/api/notifications/dispatch", headers=admin_h)
    counts = queue(client, lib)["counts"]
    assert counts == {"queued": 0, "sent": 0, "failed": 1, "cancelled": 0}  # gave up after the 3 attempts
    [row] = queue(client, lib, status="failed")["notifications"]
    assert row["attempts"] == 3 and "timed out" in row["last_error"]
    assert client.post(f"/api/notifications/{row['id']}/retry", headers=admin_h).status_code == 200
    assert client.post(f"/api/notifications/{row['id']}/retry", headers=admin_h).status_code == 409  # no longer failed
    assert client.post("/api/notifications/dispatch", headers=admin_h).json()["sent"] == 1


# ================================================================== preferences and access
@pytest.mark.req("FR09", "NFR02")
def test_notification_preferences_are_validated_and_admin_only(client, lib, admin_h):
    users = client.get("/api/notifications/prefs", headers=admin_h).json()["users"]
    uid = [u for u in users if u["username"] == "clerk"][0]["user_id"]

    def put(channels, hdr=admin_h):
        return client.put(f"/api/notifications/prefs/{uid}", headers=hdr, json={"channels": channels})

    assert put({"email": {"active": True, "address": "not an email"}}).status_code == 422
    assert put({"sms": {"active": True, "address": "12"}}).status_code == 422
    assert put({"email": {"active": True, "address": ""}}).status_code == 422  # switched on with nowhere to send
    assert put({"fax": {"active": True, "address": "123456789"}}).status_code == 422  # not a channel
    ok = put({"email": {"active": True, "address": "clerk@library.example"}, "print": {"active": False, "address": ""}})
    assert ok.status_code == 200 and ok.json()["channels"]["email"] == {
        "active": True,
        "address": "clerk@library.example",
    }
    assert client.put("/api/notifications/prefs/9999", headers=admin_h, json={"channels": {}}).status_code == 404
    assert put({}, hdr=lib).status_code == 403  # a librarian may see alerts but not change who gets them
    assert client.get("/api/notifications/prefs", headers=lib).status_code == 403
    assert client.post("/api/notifications/dispatch", headers=lib).status_code == 403
    assert "notification.prefs" in {a for (a,) in sql(client.settings, "SELECT action FROM audit_events")}


@pytest.mark.req("FR07", "NFR02")
def test_who_may_see_and_who_may_simulate(client, lib, clerk):
    anon = TestClient(client.app)
    assert anon.get("/api/gate/events").status_code == 401
    assert client.get("/api/gate/events", headers=clerk).status_code == 403  # a desk clerk has no gate.view
    auditor = login_card(client, "STAFFAUDIT01")
    assert client.get("/api/gate/events", headers=auditor).status_code == 200  # an auditor may look
    assert client.post("/api/gate/mock/gates/gate-1/walk", headers=auditor, json={}).status_code == 403
    assert client.get("/api/gate/events/9999", headers=lib).status_code == 404
    assert client.get("/gate", headers=clerk).status_code == 403


@pytest.mark.req("FR07", "NFR06")
def test_gate_trouble_is_reported_clearly(client, lib, gate):
    assert client.post("/api/gate/mock/gates/nope/walk", headers=lib, json={}).status_code == 404
    gate.online = False
    assert client.post("/api/gate/mock/gates/gate-1/walk", headers=lib, json={}).status_code == 503
    gate.online = True
    r = client.post("/api/gate/mock/gates/gate-1/walk", headers=lib, json={"item_refs": ["NOPE"]})
    assert r.status_code == 404
    make_item(client, lib, "T1")  # an untagged book cannot be seen by the gate
    r = client.post("/api/gate/mock/gates/gate-1/walk", headers=lib, json={"item_refs": ["T1"]})
    assert r.status_code == 409 and "no tag" in r.json()["detail"]["message"]


@pytest.mark.req("FR07", "NFR02")
def test_the_gate_record_cannot_be_edited(client, lib):
    tagged(client, lib, "A1")
    walk(client, lib, ["A1"])
    for statement in (
        "UPDATE gate_events SET alarm = 0", "DELETE FROM gate_events", "DELETE FROM gate_images",
        "UPDATE gate_passages SET people = 9",
    ):  # fmt: skip
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            sql(client.settings, statement)


# ================================================================== configuration, migration, pages
@pytest.mark.req("FR03", "FR09", "NFR06")
def test_gates_cameras_and_providers_come_from_configuration():
    reg = DeviceRegistry((DeviceConfig("g", "gate", "mock"), DeviceConfig("c", "camera", "mock")))
    assert {d["id"]: d["kind"] for d in reg.describe()} == {"g": "gate", "c": "camera"}
    assert reg.get("g", "gate") is not None and reg.get("g", "camera") is None
    providers = build_providers(NotificationConfig())
    assert set(providers) == {"email", "sms", "print"} and providers["sms"].channel == "sms"
    with pytest.raises(ProviderConfigError, match="unknown sms provider"):
        build_providers(NotificationConfig(sms_driver="twilio"))


@pytest.mark.req("NFR06", "NFR01")
def test_migration_0008_goes_up_and_down(tmp_path):
    url = f"sqlite:///{(tmp_path / 'g.db').as_posix()}"
    migrate.upgrade(url, "0007")
    migrate.upgrade(url)
    con = sqlite3.connect(tmp_path / "g.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gate_events", "gate_images", "gate_passages", "notifications", "notification_prefs"} <= tables
    con.close()
    migrate.downgrade(url, "0007")
    migrate.upgrade(url)


@pytest.mark.req("FR09")
def test_the_command_line_can_deliver_the_queue(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    db = tmp_path / "cli.db"
    cfg.write_text(f'[database]\nurl = "sqlite:///{db.as_posix()}"\n', encoding="utf-8")
    assert cli_main(["--config", str(cfg), "seed-demo"]) == 0
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO notifications (created_at, channel, recipient, subject, body, status, attempts, last_error, "
        "next_attempt_at, provider_ref) VALUES ('2026-01-01', 'email', 'a@b.example', 'hi', 'body', 'queued', 0, '', "
        "'2026-01-01', '')"
    )
    con.commit()
    con.close()
    assert cli_main(["--config", str(cfg), "notify-dispatch"]) == 0
    assert "'sent': 1" in capsys.readouterr().out


@pytest.mark.req("AC06")
def test_the_gate_page_shows_admin_tools_only_to_admins(client, lib, admin_h):
    page = client.get("/gate", headers=lib).text
    assert "What passes the gate" in page and "Simulate a walk through the gate" in page
    assert "Alert recipients" not in page and "Send queued messages" not in page
    admin_page = client.get("/gate", headers=admin_h).text
    assert "Alert recipients" in admin_page and "Send queued messages" in admin_page
    assert (
        "Simulate a walk through the gate" not in client.get("/gate", headers=login_card(client, "STAFFAUDIT01")).text
    )


@pytest.mark.req("FR09", "NFR06")
def test_unfinished_messages_are_listed_first_and_a_waiting_retry_is_explained(client, lib, admin_h):
    for acc in ("Q1", "Q2", "Q3"):
        tagged(client, lib, acc)
    walk(client, lib, ["Q1"])
    client.post("/api/notifications/dispatch", headers=admin_h)  # all sent
    client.post("/api/notifications/mock/fail", headers=admin_h, json={"channel": "email", "times": 1})
    walk(client, lib, ["Q2"])  # newer messages, one of which will fail its first send
    client.post("/api/notifications/dispatch", headers=admin_h)
    shown = queue(client, lib, limit=2)["notifications"]
    assert (
        shown[0]["status"] == "queued" and shown[0]["attempts"] == 1
    )  # the one needing attention is first, not buried
    assert all(n["status"] != "sent" for n in shown[:1])


@pytest.mark.req("FR09")
def test_dispatch_says_when_the_next_retry_is_due(tmp_path):
    from aisys.db import make_engine, make_sessionmaker
    from aisys.notifications import queue as nq
    from aisys.notifications.providers import NotificationConfig, build_providers

    url = f"sqlite:///{(tmp_path / 'w.db').as_posix()}"
    migrate.upgrade(url)
    cfg = NotificationConfig(backoff_s=3600)  # a retry is an hour away
    providers = build_providers(cfg)
    providers["email"].fail_next("error", 1)
    with make_sessionmaker(make_engine(url))() as db:
        nq.enqueue(db, channel="email", recipient="a@b.example", subject="s", body="b")
        db.commit()
        first = nq.dispatch(db, providers, cfg)
        assert first["retry_later"] == 1 and first["next_attempt_at"]  # it says when it will be tried again
        again = nq.dispatch(db, providers, cfg)
        assert again["tried"] == 0 and again["waiting"] == 1 and again["next_attempt_at"] == first["next_attempt_at"]
