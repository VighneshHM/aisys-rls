"""Members: add a member, issue or replace a patron card, find a member by name."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import login_card
from test_circulation import book, out, sql


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def add(client, hdr, name, **kw):
    return client.post("/api/circulation/members", headers=hdr, json={"name": name, **kw})


def identify(client, hdr, uid):
    return client.post("/api/circulation/members/identify", headers=hdr, json={"uid": uid})


@pytest.mark.req("FR04", "FR11")
def test_a_new_member_gets_the_next_number_and_can_use_a_new_card(client, lib):
    r = add(client, lib, "  Nadia   Rahman ", card_uid="patron 700001")  # spaces and case are tidied
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["member_no"] == "M0007"  # the demo data has M0001-M0006
    assert created["name"] == "Nadia Rahman" and created["status"] == "active" and created["can_borrow"] is True
    assert identify(client, lib, "PATRON700001").json()["member_no"] == "M0007"  # the card identifies her
    assert created["card"]["uid"] == "PATRON700001" and created["cards_withdrawn"] == 0  # the panel shows her card
    assert add(client, lib, "No Card Yet").json()["card"] is None
    book(client, lib, "MB1")
    assert out(client, lib, "M0007", "MB1").status_code == 200  # and she can borrow at once
    assert (
        add(client, lib, "Next Person").json()["member_no"] == "M0009"
    )  # numbers keep counting (M0008 went to "No Card Yet")


@pytest.mark.req("FR04")
def test_member_input_is_validated(client, lib):
    assert add(client, lib, "   ").status_code == 422  # a blank name
    assert add(client, lib, "Valid Name", member_no="bad number!").json()["detail"]["code"] == "bad_member_no"
    assert add(client, lib, "Valid Name", card_uid="12").json()["detail"]["code"] == "bad_card"
    dup_no = add(client, lib, "Someone", member_no="m0001")  # case-insensitive clash with the demo M0001
    assert dup_no.status_code == 409 and dup_no.json()["detail"]["code"] == "member_exists"
    dup_card = add(client, lib, "Someone", card_uid="PATRON000001")
    assert dup_card.status_code == 409 and dup_card.json()["detail"]["code"] == "card_in_use"
    staff_card = add(client, lib, "Someone", card_uid="STAFFLIB0001")  # a staff card can never become a patron card
    assert staff_card.status_code == 409
    own = add(client, lib, "Custom Number", member_no="lib-42").json()
    assert own["member_no"] == "LIB-42"  # explicit numbers are kept, in capitals
    assert add(client, lib, "After Custom").json()["member_no"] == "M0007"  # auto numbering ignores other formats


@pytest.mark.req("FR04", "FR11")
def test_a_replacement_card_switches_the_old_one_off(client, lib, settings):
    old = "PATRON000001"
    assert identify(client, lib, old).status_code == 200
    r = client.post("/api/circulation/members/M0001/card", headers=lib, json={"uid": "patron-800001"})
    assert r.status_code == 200 and r.json()["member_no"] == "M0001"
    lost = identify(client, lib, old)
    assert lost.status_code == 409 and lost.json()["detail"]["code"] == "card_inactive"
    assert "replaced" in lost.json()["detail"]["message"]
    assert identify(client, lib, "PATRON800001").json()["member_no"] == "M0001"
    offered = [c["uid"] for c in client.get("/api/circulation/mock/patron-cards", headers=lib).json()]
    assert "PATRON800001" in offered and old not in offered  # the simulator offers only working cards
    shown = client.get("/api/circulation/members/M0001", headers=lib).json()
    assert shown["card"]["uid"] == "PATRON800001" and shown["cards_withdrawn"] == 1  # current card, one withdrawn
    assert client.post("/api/auth/card-login", json={"uid": "PATRON800001"}).status_code in (401, 403)  # never a login
    taken = client.post("/api/circulation/members/M0002/card", headers=lib, json={"uid": "PATRON800001"})
    assert taken.status_code == 409  # one card, one member
    assert (
        client.post("/api/circulation/members/M9999/card", headers=lib, json={"uid": "PATRON900001"}).status_code == 404
    )
    rows = sql(settings, "SELECT uid, active FROM cards WHERE member_ref = 'M0001' ORDER BY id")
    assert rows == [(old, 0), ("PATRON800001", 1)]  # the old card is kept on record, switched off


@pytest.mark.req("FR04")
def test_members_can_be_found_by_name_or_number(client, lib):
    add(client, lib, "Priya Nairobi")
    found = client.get("/api/circulation/members", headers=lib, params={"q": "NAIR"}).json()["members"]
    assert [m["name"] for m in found] == ["Priya Nair", "Priya Nairobi"]  # case-insensitive, partial
    assert [
        m["member_no"]
        for m in client.get("/api/circulation/members", headers=lib, params={"q": "m0004"}).json()["members"]
    ] == ["M0004"]
    assert client.get("/api/circulation/members", headers=lib, params={"q": ""}).json()["members"] == []
    assert (
        client.get("/api/circulation/members", headers=lib, params={"q": "%"}).json()["members"] == []
    )  # no wildcard abuse
    assert client.get("/api/circulation/members", headers=lib, params={"q": "zzzz"}).json()["members"] == []
    sam = client.get("/api/circulation/members", headers=lib, params={"q": "ortiz"}).json()["members"][0]
    assert sam["fines_cents"] == 1250 and sam["status"] == "active"
    assert sam["card"] == "PATRON000004"  # the search shows which card each person holds
    assert (
        client.get("/api/circulation/members", headers=lib, params={"q": "priya nairobi"}).json()["members"][0]["card"]
        is None
    )


@pytest.mark.req("FR04", "NFR02")
def test_who_may_manage_members(client, lib, clerk, settings):
    anon = TestClient(client.app)
    assert anon.get("/api/circulation/members?q=a").status_code == 401
    assert add(client, clerk, "Not Allowed").status_code == 403  # a desk clerk can look people up but not add them
    assert (
        client.post("/api/circulation/members/M0001/card", headers=clerk, json={"uid": "PATRON999999"}).status_code
        == 403
    )
    assert client.get("/api/circulation/members?q=priya", headers=clerk).status_code == 200
    add(client, lib, "Audit Me", card_uid="PATRON555555")
    client.post("/api/circulation/members/M0001/card", headers=lib, json={"uid": "PATRON555556"})
    logged = {a for (a,) in sql(settings, "SELECT action FROM audit_events")}
    assert {"member.create", "member.card_issue"} <= logged


@pytest.mark.req("FR04")
def test_the_page_offers_member_tools_only_to_those_who_may_use_them(client, lib, clerk):
    page = client.get("/circulation", headers=lib).text
    for text in ("Add a new member", "Issue or replace card", "find a member by name"):
        assert text in page
    assert "Add a new member" not in client.get("/circulation", headers=clerk).text  # the clerk has no member.write


@pytest.mark.req("NFR05")
def test_ordinary_forms_do_not_use_the_red_danger_style(client, lib):
    """Add-member, card and fine forms are not warnings; only block-member and tag removal use the red box."""
    page = client.get("/circulation", headers=lib).text
    assert page.count('class: "confirm neutral"') == 4  # fine form, card form, member form, contact form
    assert page.count('class: "confirm"') == 1  # the block-member reason box
    css = client.get("/static/app.css").text
    assert ".confirm.neutral" in css


@pytest.mark.req("FR04")
def test_the_card_box_and_the_demo_list_follow_whoever_is_shown(client, lib):
    page = client.get("/circulation", headers=lib).text
    assert "function syncCardFields" in page and "syncCardFields(m);" in page  # every way of loading a member calls it
