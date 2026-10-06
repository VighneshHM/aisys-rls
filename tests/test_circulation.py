"""AC03: check out, renew and check in through the ILMS connector, with an auditable history. Also FR04 blocks."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from aisys.app import create_app
from aisys.circulation.connector import CirculationConfig, CirculationConnector
from aisys.circulation.registry import ConnectorConfigError, build_connector
from aisys.cli import seed_demo
from aisys.config import Settings
from conftest import login_card
from test_tagging import make_item, place, tag


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def ilms(client):
    return client.app.state.ilms.inner  # the mock ILMS behind the retry wrapper


@pytest.fixture
def reader(client):
    return client.app.state.devices.get("staff-1")


def book(client, lib, acc, **extra):
    return make_item(client, lib, acc, f"Title of {acc}", **extra)


def tagged_book(client, lib, acc):
    item = book(client, lib, acc)
    uid = place(client, lib)
    assert tag(client, lib, item["id"], uid).status_code == 201
    return item, uid


def out(client, hdr, member, item, **kw):
    return client.post("/api/circulation/checkout", headers=hdr, json={"member_no": member, "item_ref": item, **kw})


def renew(client, hdr, item):
    return client.post("/api/circulation/renew", headers=hdr, json={"item_ref": item})


def back(client, hdr, item):
    return client.post("/api/circulation/checkin", headers=hdr, json={"item_ref": item})


def events(client, hdr, **q):
    return client.get("/api/circulation/events", headers=hdr, params={"limit": 100, **q}).json()["events"]


def sql(settings, query, *args):
    with sqlite3.connect(settings.database_url.removeprefix("sqlite:///")) as c:
        return c.execute(query, args).fetchall()


# ================================================================== AC03: the three actions, and their history
@pytest.mark.req("AC03", "FR04")
def test_check_out_renew_and_check_in_leave_an_auditable_history(client, lib, ilms):
    book(client, lib, "C1")
    r = out(client, lib, "M0001", "C1")
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["code"] == "ok" and first["protocol"] == "sip2" and first["loan"]["member_no"] == "M0001"
    due1 = first["loan"]["due_at"]
    assert due1.endswith("+00:00")  # an explicit timezone, so a browser shows the right local time
    assert status_of(client, lib, "C1") == "on_loan"

    second = renew(client, lib, "C1").json()
    assert second["loan"]["renewals"] == 1 and second["loan"]["due_at"] >= due1

    done = back(client, lib, "C1").json()
    assert done["loan"]["returned_at"] and done["loan"]["returned_by"] == "librarian"
    assert status_of(client, lib, "C1") == "available"

    history = events(client, lib, item_ref="C1")
    assert [(e["action"], e["outcome"]) for e in reversed(history)] == [
        ("checkout", "ok"),
        ("renew", "ok"),
        ("checkin", "ok"),
    ]
    assert all(e["actor"] == "librarian" and e["member_no"] == "M0001" and e["protocol"] == "sip2" for e in history)
    # each history row carries the id the ILMS saw, so it can be matched with the ILMS's own log
    assert {e["txn_id"] for e in history} == {txn for _, txn in ilms.calls}


def status_of(client, lib, acc):
    item_id = client.get("/api/catalogue/items", headers=lib, params={"q": acc}).json()["items"][0]["id"]
    return client.get(f"/api/catalogue/items/{item_id}", headers=lib).json()["status"]


def book_id(client, lib, acc):
    return client.get("/api/catalogue/items", headers=lib, params={"q": acc}).json()["items"][0]["id"]


@pytest.mark.req("AC03")
def test_an_item_can_be_named_by_accession_barcode_or_tag(client, lib):
    item, uid = tagged_book(client, lib, "C2")
    barcoded = book(client, lib, "C3", barcode="BC-000003")
    assert out(client, lib, "M0001", uid).status_code == 200  # the tag a reader saw
    assert out(client, lib, "M0001", barcoded["barcode"]).status_code == 200
    assert (
        client.get("/api/circulation/items/resolve", headers=lib, params={"ref": uid}).json()["loan"]["member_no"]
        == "M0001"
    )
    assert out(client, lib, "M0001", "NOPE").status_code == 404
    assert out(client, lib, "M9999", "C2").status_code == 404


@pytest.mark.req("AC03", "FR04")
def test_due_date_enquiry_goes_through_the_connector(client, lib, ilms):
    book(client, lib, "C4")
    assert client.get("/api/circulation/due", headers=lib, params={"item_ref": "C4"}).json()["on_loan"] is False
    due = out(client, lib, "M0002", "C4").json()["loan"]["due_at"]
    r = client.get("/api/circulation/due", headers=lib, params={"item_ref": "C4"}).json()
    assert r["on_loan"] and r["ok"] and r["due_at"] == due and r["member_no"] == "M0002"
    assert ilms.calls[-1][0] == "due_date"


# ================================================================== the security bit on the tag
@pytest.mark.req("AC03", "FR07")
def test_the_security_bit_follows_the_loan(client, lib, reader):
    item, uid = tagged_book(client, lib, "S1")
    assert reader.memory_of(uid).armed is True
    r = out(client, lib, "M0001", "S1").json()
    assert r["security"]["status"] == "disarmed" and reader.memory_of(uid).armed is False
    assert renew(client, lib, "S1").json()["security"]["status"] == "unchanged"
    r = back(client, lib, "S1").json()
    assert r["security"]["status"] == "armed" and reader.memory_of(uid).armed is True
    assert events(client, lib, item_ref="S1")[0]["detail"]["security"]["status"] == "armed"


@pytest.mark.req("AC03", "FR07")
def test_a_tag_that_is_not_on_the_reader_never_blocks_the_loan(client, lib, reader):
    item, uid = tagged_book(client, lib, "S2")
    reader.remove_tag(uid)
    r = out(client, lib, "M0001", "S2")
    assert r.status_code == 200
    assert r.json()["security"]["status"] == "not_on_reader" and "gate" in r.json()["security"]["message"]
    reader.online = False
    r = back(client, lib, "S2")
    assert r.status_code == 200 and r.json()["security"]["status"] == "reader_offline"
    book(client, lib, "S3")  # an untagged copy can be lent too
    assert out(client, lib, "M0001", "S3").json()["security"]["status"] == "no_tag"


# ================================================================== FR04 blocks
@pytest.mark.req("AC04", "FR04")
def test_circulation_is_blocked_for_reference_items_blocked_members_and_big_fines(client, lib, settings):
    book(client, lib, "R1", item_type="reference")
    book(client, lib, "B1")
    cases = [("M0001", "R1", "reference_item"), ("M0003", "B1", "patron_blocked"), ("M0004", "B1", "fines_exceeded")]
    for member, item, code in cases:
        r = out(client, lib, member, item)
        assert r.status_code == 409 and r.json()["detail"]["code"] == code, (member, item, r.text)
        last = events(client, lib)[0]
        assert (last["outcome"], last["code"], last["detail"]["source"]) == ("refused", code, "local")
    assert sql(settings, "SELECT COUNT(*) FROM loans") == [(0,)]  # nothing was lent
    assert sql(settings, "SELECT status FROM items WHERE accession_no='B1'") == [("available",)]
    assert out(client, lib, "M0005", "B1").status_code == 200  # owes 3.00, under the 5.00 limit: allowed


@pytest.mark.req("AC04", "FR04")
def test_a_librarian_can_block_and_unblock_a_member(client, lib, clerk, settings):
    book(client, lib, "B2")
    assert client.post("/api/circulation/members/M0006/block", headers=clerk, json={"blocked": True}).status_code == 403
    r = client.post("/api/circulation/members/M0006/block", headers=lib, json={"blocked": True, "reason": "lost card"})
    assert r.json()["status"] == "blocked" and r.json()["can_borrow"] is False
    refused = out(client, lib, "M0006", "B2")
    assert refused.json()["detail"]["code"] == "patron_blocked" and "lost card" in refused.json()["detail"]["message"]
    client.post("/api/circulation/members/M0006/block", headers=lib, json={"blocked": False})
    assert out(client, lib, "M0006", "B2").status_code == 200
    logged = {a for (a,) in sql(settings, "SELECT action FROM audit_events")}
    assert {"member.block", "member.unblock"} <= logged


@pytest.mark.req("AC03", "FR04")
def test_sensible_state_errors_for_renew_and_check_in(client, lib):
    book(client, lib, "E1")
    assert renew(client, lib, "E1").json()["detail"]["code"] == "not_on_loan"
    assert back(client, lib, "E1").json()["detail"]["code"] == "not_on_loan"
    assert out(client, lib, "M0001", "E1").status_code == 200
    again = out(client, lib, "M0002", "E1")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "item_on_loan"
    client.post("/api/circulation/members/M0001/block", headers=lib, json={"blocked": True, "reason": "x"})
    assert renew(client, lib, "E1").json()["detail"]["code"] == "patron_blocked"  # a blocked member cannot renew
    assert back(client, lib, "E1").status_code == 200  # but can always return


@pytest.mark.req("AC03", "FR04")
def test_the_ilms_owns_the_loan_policy(client, lib):
    for n in range(1, 8):
        book(client, lib, f"P{n}")
    assert out(client, lib, "M0001", "P1").status_code == 200
    for _ in range(2):
        assert renew(client, lib, "P1").status_code == 200
    third = renew(client, lib, "P1")
    assert third.status_code == 409 and third.json()["detail"]["code"] == "renewal_limit"
    assert "maximum renewals" in third.json()["detail"]["message"].lower()  # SIP2 wording
    assert events(client, lib)[0]["detail"]["source"] == "ilms"
    for n in range(2, 6):
        assert out(client, lib, "M0001", f"P{n}").status_code == 200  # five loans in all
    sixth = out(client, lib, "M0001", "P6")
    assert sixth.status_code == 409 and sixth.json()["detail"]["code"] == "loan_limit"
    member = client.get("/api/circulation/members/M0001", headers=lib).json()
    assert member["open_loans"] == 5 and len(member["loans"]) == 5


@pytest.mark.req("AC03", "FR03")
def test_the_same_flow_works_in_ncip_wording(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'n.db').as_posix()}",
        log_level="WARNING",
        circulation=CirculationConfig(ilms_protocol="ncip", ilms_backoff_s=0, max_renewals=0),
    )
    app = create_app(settings)
    seed_demo(settings)
    with TestClient(app) as c:
        hdr = login_card(c, "STAFFLIB0001")
        book(c, hdr, "N1")
        assert out(c, hdr, "M0001", "N1").json()["protocol"] == "ncip"
        refused = renew(c, hdr, "N1")
        assert refused.json()["detail"]["message"] == "Maximum Renewals Exceeded"
        assert events(c, hdr)[0]["protocol"] == "ncip"


# ================================================================== retries, timeouts, duplicates
@pytest.mark.req("AC03", "FR03", "NFR06")
def test_a_timeout_is_retried_and_the_loan_happens_once(client, lib, ilms, settings):
    book(client, lib, "T1")
    ilms.fail_next("timeout", times=2)  # two failures, then the third attempt succeeds
    r = out(client, lib, "M0001", "T1")
    assert r.status_code == 200
    assert sql(settings, "SELECT COUNT(*) FROM loans") == [(1,)]
    assert len({txn for _, txn in ilms.calls}) == 1 and len(ilms.calls) == 3  # one transaction id, three attempts


@pytest.mark.req("AC03", "FR03", "NFR06")
def test_a_lost_reply_does_not_lend_twice(client, lib, ilms, settings):
    book(client, lib, "T2")
    ilms.fail_next("lost_reply")  # the ILMS processed the request but its answer never arrived
    r = out(client, lib, "M0001", "T2")
    assert r.status_code == 200 and r.json()["replayed"] is True  # the retry was recognised, not repeated
    assert sql(settings, "SELECT COUNT(*) FROM loans") == [(1,)]
    assert len([e for e in events(client, lib) if e["outcome"] == "ok"]) == 1


@pytest.mark.req("AC03", "NFR06")
def test_when_the_ilms_stays_down_nothing_changes_and_the_attempt_is_recorded(client, lib, ilms, settings):
    book(client, lib, "T3")
    ilms.fail_next("error", times=3)  # the first try plus both retries
    r = out(client, lib, "M0001", "T3")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "ilms_unavailable"
    assert sql(settings, "SELECT COUNT(*) FROM loans") == [(0,)]
    assert sql(settings, "SELECT status FROM items WHERE accession_no='T3'") == [("available",)]
    last = events(client, lib)[0]
    assert (last["action"], last["outcome"], last["code"]) == ("checkout", "failed", "ilms_unavailable")
    assert out(client, lib, "M0001", "T3").status_code == 200  # the next attempt works


# ================================================================== integrity and access
@pytest.mark.req("AC03", "NFR02")
def test_the_history_cannot_be_edited_and_an_item_cannot_have_two_open_loans(client, lib, settings):
    book(client, lib, "I1")
    out(client, lib, "M0001", "I1")
    for statement in ("UPDATE circ_events SET outcome='x'", "DELETE FROM circ_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            sql(settings, statement)
    with pytest.raises(sqlite3.IntegrityError):  # the database itself refuses a second open loan
        sql(
            settings,
            "INSERT INTO loans (member_id, item_id, out_at, due_at, out_by) "
            "SELECT 2, item_id, out_at, due_at, 'x' FROM loans",
        )


@pytest.mark.req("AC03", "NFR02", "FR11")
def test_permissions_and_patron_cards(client, lib, clerk):
    book(client, lib, "A1")
    anon = TestClient(client.app)  # no session cookie
    assert anon.post("/api/circulation/checkout", json={"member_no": "M0001", "item_ref": "A1"}).status_code == 401
    auditor = login_card(client, "STAFFAUDIT01")
    assert out(client, auditor, "M0001", "A1").status_code == 403
    assert client.get("/api/circulation/events", headers=auditor).status_code == 200  # may read the history
    assert out(client, clerk, "M0001", "A1").status_code == 200  # the desk clerk can lend
    # a patron card identifies a member but can never sign anyone in
    r = client.post("/api/auth/card-login", json={"uid": "PATRON000001"})
    assert r.status_code in (401, 403)
    who = client.post("/api/circulation/members/identify", headers=clerk, json={"uid": "PATRON000001"}).json()
    assert who["member_no"] == "M0001" and who["open_loans"] == 1 and who["can_borrow"] is True
    assert (
        client.post("/api/circulation/members/identify", headers=clerk, json={"uid": "STAFFLIB0001"}).status_code == 404
    )
    assert anon.get("/api/circulation/members/M0001").status_code == 401


@pytest.mark.req("AC03", "NFR06")
def test_the_connector_is_chosen_by_configuration():
    with pytest.raises(ConnectorConfigError, match="unknown ILMS driver"):
        build_connector(CirculationConfig(ilms_driver="nope"))
    assert isinstance(build_connector(CirculationConfig()).inner, CirculationConnector)


@pytest.mark.req("AC03")
def test_the_circulation_page_is_gated(client, clerk):
    assert TestClient(client.app).get("/circulation", follow_redirects=False).status_code in (302, 303, 307)
    page = client.get("/circulation", headers=clerk)
    assert page.status_code == 200 and "Check out" in page.text
    auditor = login_card(client, "STAFFAUDIT01")
    assert client.get("/circulation", headers=auditor).status_code == 403


@pytest.mark.req("AC03")
def test_the_simulator_helpers_work_in_development(client, lib, reader):
    item, uid = tagged_book(client, lib, "D1")
    reader.clear_pad()
    r = client.post("/api/circulation/mock/present", headers=lib, json={"item_ref": "D1"}).json()
    assert r["on_reader"] and reader.memory_of(uid) is not None
    cards = client.get("/api/circulation/mock/patron-cards", headers=lib).json()
    assert {"PATRON000003", "M0003"} <= {v for c in cards for v in c.values()}
    assert client.post("/api/circulation/mock/ilms/fail", headers=lib, json={"mode": "bogus"}).status_code == 422


@pytest.mark.req("AC03", "NFR06")
def test_simulated_failures_can_be_counted_and_cleared(client, lib, ilms):
    book(client, lib, "F1")
    r = client.post("/api/circulation/mock/ilms/fail", headers=lib, json={"mode": "error", "times": 3}).json()
    assert r["pending"] == 3
    assert out(client, lib, "M0001", "F1").status_code == 503  # all three tries used
    assert client.get("/api/circulation/mock/ilms/fail", headers=lib).json()["pending"] == 0
    client.post("/api/circulation/mock/ilms/fail", headers=lib, json={"mode": "error", "times": 5})
    assert client.delete("/api/circulation/mock/ilms/fail", headers=lib).json()["pending"] == 0
    assert out(client, lib, "M0001", "F1").status_code == 200  # nothing left to fail
