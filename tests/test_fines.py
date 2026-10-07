"""Fines: charged on a late return, paid or waived by staff, every change a ledger row."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aisys import migrate
from aisys.circulation.connector import CirculationConfig
from aisys.circulation.fines import assess
from conftest import login_card
from test_circulation import back, book, events, out, renew, sql


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def age(settings, accession, days, hours=0):
    """Make an open loan overdue: move its due time `days` (and `hours`) into the past."""
    due = (datetime.now(UTC) - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S.%f")
    sql(
        settings,
        "UPDATE loans SET due_at = ? WHERE returned_at IS NULL "
        "AND item_id = (SELECT id FROM items WHERE accession_no = ?)",
        due,
        accession,
    )


def member(client, hdr, no):
    return client.get(f"/api/circulation/members/{no}", headers=hdr).json()


def ledger(client, hdr, no):
    return client.get(f"/api/circulation/members/{no}/fines", headers=hdr).json()


def pay(client, hdr, no, cents, note=""):
    return client.post(
        f"/api/circulation/members/{no}/fines/payment", headers=hdr, json={"amount_cents": cents, "note": note}
    )


def waive(client, hdr, no, cents, note=""):
    return client.post(
        f"/api/circulation/members/{no}/fines/waiver", headers=hdr, json={"amount_cents": cents, "note": note}
    )


# ================================================================== the rule
@pytest.mark.req("FR04")
def test_the_fine_rule_has_a_grace_period_a_daily_rate_and_a_cap():
    cfg = CirculationConfig()  # 0.25 a day, 1 day grace, 10.00 cap
    due = datetime(2026, 1, 1, 12, tzinfo=UTC)

    def cost(days, hours=0):
        return assess(due, due + timedelta(days=days, hours=hours), cfg)

    assert cost(0)["amount_cents"] == 0 and cost(0, 23)["days_late"] == 0  # a part day is not a day late
    assert cost(1)["amount_cents"] == 0 and cost(1)["days_late"] == 1  # the grace day is free
    assert cost(2)["amount_cents"] == 25 and cost(5)["amount_cents"] == 100
    big = cost(500)
    assert big["amount_cents"] == 1000 and big["capped"] is True  # never more than the cap
    assert assess(due, due - timedelta(days=3), cfg)["amount_cents"] == 0  # early is never negative
    flat = CirculationConfig(grace_days=0, fine_per_day_cents=100, max_fine_per_loan_cents=10_000)
    assert assess(due, due + timedelta(days=3), flat)["amount_cents"] == 300  # the rule follows the config


# ================================================================== charged on return
@pytest.mark.req("FR04", "AC03")
def test_a_late_return_is_charged_and_an_on_time_one_is_not(client, lib, settings):
    book(client, lib, "F1")
    book(client, lib, "F2")
    out(client, lib, "M0001", "F1")
    out(client, lib, "M0001", "F2")
    age(settings, "F1", days=6)  # 6 days late: 5 billable at 0.25 = 1.25
    late = back(client, lib, "F1").json()
    assert late["fine"]["amount_cents"] == 125 and late["fine"]["billable_days"] == 5
    assert "5 day(s) late after 1 grace" in late["fine"]["note"] and late["fine"]["balance_cents"] == 125
    assert late["was_overdue"] is True
    on_time = back(client, lib, "F2").json()
    assert on_time["fine"] is None and on_time["was_overdue"] is False  # returned on time
    assert member(client, lib, "M0001")["fines_cents"] == 125
    [entry] = ledger(client, lib, "M0001")["entries"]
    assert (entry["kind"], entry["amount_cents"], entry["actor"]) == ("charge", 125, "librarian") and entry["loan_id"]
    assert events(client, lib, item_ref="F1")[0]["detail"]["fine_cents"] == 125  # in the transaction history too


@pytest.mark.req("FR04")
def test_an_open_loan_shows_the_fine_so_far_and_renewing_cannot_erase_it(client, lib, settings):
    book(client, lib, "F3")
    out(client, lib, "M0002", "F3")
    age(settings, "F3", days=10)
    shown = member(client, lib, "M0002")["loans"][0]
    assert shown["overdue"] and shown["fine_so_far_cents"] == 225  # 9 billable days, shown but not yet charged
    assert member(client, lib, "M0002")["fines_cents"] == 0
    renewed = renew(client, lib, "F3")  # renewing restarts the clock, so the fine so far is charged first
    assert renewed.status_code == 200 and renewed.json()["fine"]["amount_cents"] == 225
    assert member(client, lib, "M0002")["fines_cents"] == 225
    [entry] = ledger(client, lib, "M0002")["entries"]
    assert entry["kind"] == "charge" and entry["amount_cents"] == 225
    assert member(client, lib, "M0002")["loans"][0]["fine_so_far_cents"] == 0  # a fresh due date: nothing accruing
    again = renew(client, lib, "F3").json()  # a second renewal on time charges nothing more
    assert again["fine"] is None and member(client, lib, "M0002")["fines_cents"] == 225
    resolved = client.get("/api/circulation/items/resolve", headers=lib, params={"ref": "F3"}).json()
    assert "fine_so_far_cents" in resolved["loan"]


# ================================================================== the limit, payments and waivers
@pytest.mark.req("FR04", "AC04")
def test_a_big_fine_blocks_circulation_until_it_is_paid_down(client, lib, settings):
    book(client, lib, "F4")
    book(client, lib, "F5")
    out(client, lib, "M0001", "F4")
    age(settings, "F4", days=400)  # capped at 10.00, over the 5.00 limit
    assert back(client, lib, "F4").json()["fine"]["capped"] is True
    refused = out(client, lib, "M0001", "F5")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "fines_exceeded"
    paid = pay(client, lib, "M0001", 600, "paid at the desk").json()
    assert paid["fines_cents"] == 400 and paid["can_borrow"] is True  # 4.00 left: under the limit
    assert out(client, lib, "M0001", "F5").status_code == 200
    kinds = [e["kind"] for e in ledger(client, lib, "M0001")["entries"]]
    assert kinds == ["payment", "charge"]


@pytest.mark.req("FR04", "NFR02")
def test_payments_and_waivers_are_checked_and_need_permission(client, lib, clerk):
    assert member(client, lib, "M0004")["fines_cents"] == 1250  # the demo member who owes 12.50
    assert pay(client, clerk, "M0004", 100).status_code == 403  # a desk clerk cannot take or waive money
    assert waive(client, clerk, "M0004", 100, "x").status_code == 403
    assert pay(client, lib, "M0004", 0).status_code == 422
    assert pay(client, lib, "M0004", -5).status_code == 422
    over = pay(client, lib, "M0004", 5000)
    assert over.status_code == 409 and over.json()["detail"]["code"] == "over_balance"
    no_reason = waive(client, lib, "M0004", 100, "  ")
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "reason_required"
    assert waive(client, lib, "M0004", 750, "library error").json()["fines_cents"] == 500
    assert pay(client, lib, "M0004", 500).json()["fines_cents"] == 0
    assert [e["kind"] for e in ledger(client, lib, "M0004")["entries"]] == ["payment", "waiver", "opening"]
    assert pay(client, lib, "M9999", 100).status_code == 404


@pytest.mark.req("FR04", "NFR02")
def test_the_ledger_always_explains_the_balance_and_cannot_be_edited(client, lib, settings):
    book(client, lib, "F6")
    out(client, lib, "M0005", "F6")  # M0005 starts owing 3.00
    age(settings, "F6", days=4)
    back(client, lib, "F6")  # +0.75
    pay(client, lib, "M0005", 100)
    waive(client, lib, "M0005", 50, "goodwill")
    rows = sql(
        settings,
        "SELECT m.member_no, m.fines_cents, COALESCE(SUM(CASE WHEN f.kind IN ('opening','charge') "
        "THEN f.amount_cents ELSE -f.amount_cents END), 0) FROM members m "
        "LEFT JOIN fine_entries f ON f.member_id = m.id GROUP BY m.id",
    )
    assert rows and all(stored == from_ledger for _, stored, from_ledger in rows), rows  # no drift, for anyone
    assert dict((no, stored) for no, stored, _ in rows)["M0005"] == 300 + 75 - 100 - 50
    for statement in ("UPDATE fine_entries SET amount_cents = 1", "DELETE FROM fine_entries"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            sql(settings, statement)
    logged = {a for (a,) in sql(settings, "SELECT action FROM audit_events")}
    assert {"fine.payment", "fine.waiver"} <= logged  # staff actions are in the audit log as well


@pytest.mark.req("NFR06", "NFR01")
def test_migration_0007_turns_existing_balances_into_opening_entries(tmp_path):
    url = f"sqlite:///{(tmp_path / 'f.db').as_posix()}"
    migrate.upgrade(url, "0006")
    con = sqlite3.connect(tmp_path / "f.db")
    con.execute(
        "INSERT INTO members (member_no, name, status, fines_cents, created_at) "
        "VALUES ('X1','A','active',450,'2026-01-01')"
    )
    con.execute(
        "INSERT INTO members (member_no, name, status, fines_cents, created_at) "
        "VALUES ('X2','B','active',0,'2026-01-01')"
    )
    con.commit()
    con.close()
    migrate.upgrade(url)
    con = sqlite3.connect(tmp_path / "f.db")
    assert con.execute("SELECT kind, amount_cents, note FROM fine_entries").fetchall() == [
        ("opening", 450, "Opening balance")
    ]
    con.close()
    migrate.downgrade(url, "0006")
    migrate.upgrade(url)  # and it can go forward again


@pytest.mark.req("FR04")
def test_the_circulation_page_has_the_fine_controls(client, lib):
    page = client.get("/circulation", headers=lib).text
    for text in ("Record payment", "Waive fine", "Fine history", "fine so far"):
        assert text in page
