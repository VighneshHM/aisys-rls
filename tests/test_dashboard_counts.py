"""The dashboard's counts: patrons and the other numbers worth knowing, shown only to roles that may see them."""

from __future__ import annotations

import re

import pytest

from conftest import login_card


def tiles(client, headers) -> dict[str, int]:
    html = client.get("/dashboard", headers=headers).text
    found = re.findall(r'<div class="num">([\d,]+)</div><div class="lbl">([^<]+)</div>', html)
    return {label: int(num.replace(",", "")) for num, label in found}


@pytest.mark.req("AC08", "FR04")
def test_the_dashboard_counts_patrons_and_leaves_removed_ones_out(client):
    lib = login_card(client, "STAFFLIB0001")
    before = tiles(client, lib)
    assert before["Patrons"] == 6  # the minimal demo: six patrons, one blocked, two owing a fine
    assert before["Patrons blocked"] == 1
    assert before["Patrons owing fines"] == 2
    assert before["Patrons with items out"] == 0
    r = client.post("/api/members/M0006/remove", json={"reason": "moved"}, headers=lib)
    assert r.status_code == 200, r.text
    assert tiles(client, lib)["Patrons"] == 5


@pytest.mark.req("AC08")
def test_the_other_counts_follow_the_work(client):
    lib = login_card(client, "STAFFLIB0001")
    base = tiles(client, lib)
    assert base["Items without a tag"] == base["Catalogue items"] - base["Items with a tag"]
    assert "Gate alarms (24 h)" in base and "Messages waiting to send" in base


@pytest.mark.req("AC08", "NFR02")
def test_each_role_sees_only_the_counts_it_may_see(client):
    aud = tiles(
        client, login_card(client, "STAFFAUDIT01")
    )  # the auditor: no catalogue or patron rights, may view the gate
    assert "Patrons" not in aud and "Catalogue items" not in aud and "Gate alarms (24 h)" in aud
    clerk = tiles(client, login_card(client, "STAFFCLERK01"))  # the clerk: patrons yes, gate no
    assert "Patrons" in clerk and "Gate alarms (24 h)" not in clerk
