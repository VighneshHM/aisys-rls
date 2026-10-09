"""FR05: a member record is validated before a card is linked to it (the member side of tagging and re-tagging)."""

from __future__ import annotations

import pytest

from conftest import login_card

pytestmark = pytest.mark.req("FR05")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def issue(client, lib, member, uid):
    return client.post(f"/api/circulation/members/{member}/card", json={"uid": uid}, headers=lib)


def test_a_card_is_only_linked_to_a_member_who_exists(client, lib):
    assert issue(client, lib, "M9999", "PATRON778899").status_code == 404
    assert issue(client, lib, "M0001", "PATRON778899").status_code == 200


@pytest.mark.parametrize("uid", ["", "ab", "has!bad*chars", "x" * 80])
def test_a_bad_card_number_is_refused(client, lib, uid):
    assert issue(client, lib, "M0001", uid).status_code == 422


def test_a_card_number_cannot_belong_to_two_people(client, lib):
    assert issue(client, lib, "M0002", "PATRON000001").status_code == 409  # already Priya's
    assert issue(client, lib, "M0002", "STAFFADMIN01").status_code == 409  # a staff card is not a patron card


def test_re_tagging_a_member_withdraws_the_old_card(client, lib):
    assert issue(client, lib, "M0001", "PATRON445566").status_code == 200
    m = client.get("/api/circulation/members/M0001", headers=lib).json()
    assert m["card"]["uid"] == "PATRON445566" and m["cards_withdrawn"] >= 1
    assert (
        client.post("/api/circulation/members/identify", json={"uid": "PATRON000001"}, headers=lib).status_code == 409
    ), "the old card is dead"
    assert (
        client.post("/api/circulation/members/identify", json={"uid": "PATRON445566"}, headers=lib).status_code == 200
    )


def test_a_blocked_or_unknown_card_does_not_identify_anyone(client, lib):
    assert client.post("/api/circulation/members/identify", json={"uid": "NOTACARD123"}, headers=lib).status_code == 404
    assert (
        client.post("/api/circulation/members/identify", json={"uid": "STAFFADMIN01"}, headers=lib).status_code == 404
    )


def validate(client, headers, member, uid):
    return client.post(f"/api/circulation/members/{member}/card/validate", json={"uid": uid}, headers=headers)


def test_the_dry_run_reports_every_problem_and_writes_nothing(client, lib):
    r = validate(client, lib, "M0001", "ab").json()
    assert not r["ok"] and [p["code"] for p in r["problems"]] == ["bad_card"]
    assert r["member"]["member_no"] == "M0001" and r["current_card"] == "PATRON000001"
    assert "will_replace_old_card" in [w["code"] for w in r["warnings"]], "re-tagging a member is announced"
    assert validate(client, lib, "M9999", "PATRON778899").json()["problems"][0]["code"] == "no_such_member"
    blocked = validate(client, lib, "M0003", "PATRON778899").json()
    assert blocked["ok"] and "member_blocked" in [w["code"] for w in blocked["warnings"]]
    good = validate(client, lib, "m0002", "patron-77-88-99").json()
    assert good["ok"] and good["card_uid"] == "PATRON778899"
    m = client.get("/api/circulation/members/M0002", headers=lib).json()
    assert m["card"]["uid"] == "PATRON000002", "a dry run changes nothing"


def test_the_dry_run_needs_member_write(client):
    clerk = login_card(client, "STAFFCLERK01")
    assert validate(client, clerk, "M0001", "PATRON778899").status_code == 403


def test_the_administration_page_validates_a_member_card_the_same_way(client, admin):
    def admin_issue(member, uid):
        return client.post("/api/admin/cards", json={"uid": uid, "kind": "patron", "member_ref": member}, headers=admin)

    assert admin_issue("M9999", "PATRON778899").status_code == 422
    assert admin_issue("M0002", "PATRON000001").status_code == 409  # already Priya's
    assert admin_issue("M0002", "STAFFADMIN01").status_code == 409  # a staff card is never a patron card
    assert admin_issue("M0002", "PATRON778899").status_code == 201
    m = client.get("/api/circulation/members/M0002", headers=admin).json()
    assert m["card"]["uid"] == "PATRON778899" and m["cards_withdrawn"] >= 1, "re-tagging switches the old card off"
    assert (
        client.post("/api/circulation/members/identify", json={"uid": "PATRON000002"}, headers=admin).status_code == 409
    )
