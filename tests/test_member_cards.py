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
