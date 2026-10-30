"""AC08: dashboard statistics and filterable reports (FR08)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import login_card
from shelfsense.reports.core import MAX_DAYS_CHART, per_day
from test_circulation import back, out, renew, sql
from test_fines import age, pay
from test_tagging import make_item, place, tag

COUNTS = (
    "SELECT (SELECT COUNT(*) FROM circ_events), (SELECT COUNT(*) FROM audit_events), "
    "(SELECT COUNT(*) FROM fine_entries)"
)
TODAY = datetime.now(UTC).date()
D = TODAY.isoformat()


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def world(client, lib):
    """Four books (three tagged, one of those later untagged), five circulation attempts, a payment, a gate alarm,
    an authorised exit and one inventory sweep. The numbers asserted below follow from exactly this."""
    uids = {}
    for acc, loc, tagged in (
        ("R1", "Shelf-1", True),
        ("R2", "Shelf-1", True),
        ("R3", "Shelf-2", True),
        ("R4", "Shelf-2", False),
    ):
        item = make_item(client, lib, acc, f"Report Book {acc}", location=loc)
        if tagged:
            uids[acc] = place(client, lib)
            assert tag(client, lib, item["id"], uids[acc]).status_code == 201
    client.post(f"/api/rfid/tags/{uids['R3']}/remove", headers=lib, json={})  # R3's tag comes off
    assert out(client, lib, "M0001", "R1").status_code == 200
    assert out(client, lib, "M0001", "R2").status_code == 200
    assert back(client, lib, "R1").status_code == 200
    assert renew(client, lib, "R2").status_code == 200
    assert out(client, lib, "M0003", "R4").status_code == 409  # a blocked member is refused
    assert pay(client, lib, "M0004", 200).status_code == 200  # M0004 owed 12.50
    walked = client.post("/api/gate/mock/gates/gate-1/walk", headers=lib, json={"item_refs": ["R1"]}).json()["events"]
    assert walked[0]["kind"] == "unauthorised"  # R1 came back, so its bit is on again and it is not on loan
    client.post("/api/gate/mock/gates/gate-1/walk", headers=lib, json={"item_refs": ["R2"], "people": 2})  # authorised
    sid = client.post("/api/inventory/sessions", headers=lib, json={"location": "Shelf-1"}).json()["id"]
    client.post("/api/inventory/mock/handhelds/handheld-1/load-shelf", headers=lib, json={"session_id": sid})
    assert client.post(f"/api/inventory/sessions/{sid}/scan", headers=lib).status_code == 200
    return uids


def rep(client, hdr, report, **params):
    r = client.get(f"/api/reports/{report}", headers=hdr, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def stats(report: dict) -> dict:
    return {s["label"]: s["value"] for s in report["summary"]}


# ================================================================== the catalogue and the seven reports
@pytest.mark.req("AC08", "FR08")
def test_there_are_seven_reports_each_with_filters_and_columns(client, lib):
    cat = client.get("/api/reports", headers=lib).json()["reports"]
    assert [r["id"] for r in cat] == [
        "tagged_items",
        "members",
        "circulation",
        "operators",
        "rfid_clients",
        "fines",
        "gate_events",
    ]
    assert all(r["title"] and r["columns"] and r["description"] for r in cat)
    assert "from" in {f["key"] for f in next(r for r in cat if r["id"] == "circulation")["filters"]}  # date filters


@pytest.mark.req("AC08", "FR08")
def test_tagged_items_report_and_its_filters(client, lib, world):
    r = rep(client, lib, "tagged_items")
    assert r["total"] == 3 and {row["accession_no"] for row in r["rows"]} == {"R1", "R2", "R3"}
    s = stats(r)
    assert (s["Tag links"], s["In use"], s["Removed"], s["Retired"]) == (3, 2, 1, 0)
    assert s["Items with a tag"].startswith("2 of 4")  # R1, R2 carry a tag out of four items
    assert {p["label"]: p["value"] for p in r["chart"]["points"]} == {"Shelf-1": 2, "Shelf-2": 1}
    only = rep(client, lib, "tagged_items", state="removed")
    assert [row["accession_no"] for row in only["rows"]] == ["R3"] and only["rows"][0]["state"] == "Removed"
    assert rep(client, lib, "tagged_items", location="shelf-1")["total"] == 2  # case-insensitive
    assert rep(client, lib, "tagged_items", tagged_by="librarian")["total"] == 3
    assert rep(client, lib, "tagged_items", tagged_by="clerk")["total"] == 0
    assert rep(client, lib, "tagged_items", q="report book r2")["total"] == 1
    assert rep(client, lib, "tagged_items", **{"from": D, "to": D})["total"] == 3
    assert rep(client, lib, "tagged_items", **{"from": (TODAY + timedelta(days=1)).isoformat()})["total"] == 0


@pytest.mark.req("AC08", "FR08")
def test_members_report_counts_fines_loans_and_overdue(client, lib, world, settings):
    r = rep(client, lib, "members")
    assert r["total"] == 6
    s = stats(r)
    assert (s["Blocked"], s["Owing"], s["Total owed"]) == (1, 2, 1050 + 300)  # M0004 owes 10.50 after paying 2.00
    assert [row["member_no"] for row in rep(client, lib, "members", status="blocked")["rows"]] == ["M0003"]
    assert [row["member_no"] for row in rep(client, lib, "members", owes="over")["rows"]] == [
        "M0004"
    ]  # over the 5.00 limit
    assert {row["member_no"] for row in rep(client, lib, "members", owes="yes")["rows"]} == {"M0004", "M0005"}
    m1 = next(row for row in r["rows"] if row["member_no"] == "M0001")
    assert (m1["open_loans"], m1["overdue_loans"], m1["card"]) == (1, 0, "PATRON000001")
    age(settings, "R2", days=5)
    late = rep(client, lib, "members", has_overdue="1")
    assert [row["member_no"] for row in late["rows"]] == ["M0001"] and late["rows"][0]["overdue_loans"] == 1
    assert rep(client, lib, "members", q="sam")["rows"][0]["member_no"] == "M0004"
    assert {p["label"] for p in r["chart"]["points"]} == {"Sam Ortiz", "Ana Silva"}  # the largest balances


@pytest.mark.req("AC08", "FR08")
def test_circulation_report_filters_summary_and_per_day_chart(client, lib, world):
    r = rep(client, lib, "circulation")
    assert r["total"] == 5
    s = stats(r)
    assert (s["Transactions"], s["Done"], s["Refused"], s["Failed"], s["Members"], s["Items"]) == (5, 4, 1, 0, 2, 3)
    assert [p["value"] for p in r["chart"]["points"]] == [5]  # all five happened today
    assert rep(client, lib, "circulation", action="checkout")["total"] == 3
    assert rep(client, lib, "circulation", outcome="refused")["rows"][0]["member_no"] == "M0003"
    assert rep(client, lib, "circulation", member_no="m0001")["total"] == 4
    assert rep(client, lib, "circulation", operator="LIBRARIAN")["total"] == 5
    assert rep(client, lib, "circulation", operator="clerk")["total"] == 0
    assert rep(client, lib, "circulation", q="r2")["total"] == 2  # check-out and renewal of R2
    spread = rep(client, lib, "circulation", **{"from": (TODAY - timedelta(days=3)).isoformat(), "to": D})
    assert [p["value"] for p in spread["chart"]["points"]] == [0, 0, 0, 5]  # quiet days are shown as zero
    assert rep(client, lib, "circulation", **{"to": (TODAY - timedelta(days=1)).isoformat()})["total"] == 0
    paged = rep(client, lib, "circulation", limit=2, offset=4)
    assert paged["total"] == 5 and len(paged["rows"]) == 1


@pytest.mark.req("AC08", "FR08")
def test_operators_report_shows_what_each_person_did(client, lib, world):
    r = rep(client, lib, "operators")
    assert r["total"] == 4  # every staff user, busy or not
    top = r["rows"][0]
    assert top["username"] == "librarian"
    assert (top["checkouts"], top["renewals"], top["checkins"], top["refused"]) == (2, 1, 1, 1)
    assert (top["tags_linked"], top["inventory_sessions"], top["payments"], top["waivers"]) == (3, 1, 1, 0)
    assert top["total"] == 10
    assert stats(r)["Active in this period"] == 1 and stats(r)["Busiest"].startswith("Leo Librarian (10)")
    assert [row["username"] for row in rep(client, lib, "operators", operator="clerk")["rows"]] == ["clerk"]
    assert rep(client, lib, "operators", operator="clerk")["rows"][0]["total"] == 0
    assert rep(client, lib, "operators", **{"from": (TODAY + timedelta(days=1)).isoformat()})["rows"][0]["total"] == 0


@pytest.mark.req("AC08", "FR08", "FR03")
def test_rfid_clients_report_covers_every_device_kind(client, lib, world):
    r = rep(client, lib, "rfid_clients")
    rows = {row["device"]: row for row in r["rows"]}
    assert set(rows) == {"staff-1", "handheld-1", "gate-1", "camera-1", "card-printer-1"}
    assert (
        rows["gate-1"]["activity"] == 2
        and "3 people" in rows["gate-1"]["detail"]
        and "1 alarms" in rows["gate-1"]["detail"]
    )
    assert rows["card-printer-1"]["activity"] == 0 and rows["card-printer-1"]["unit"] == "cards printed"
    assert rows["camera-1"]["activity"] == 1 and rows["camera-1"]["unit"] == "snapshots"  # one photo, for the one alarm
    assert rows["handheld-1"]["activity"] == 1 and "1 sessions" in rows["handheld-1"]["detail"]
    assert (
        rows["staff-1"]["activity"] >= 4
        and "3 linked" in rows["staff-1"]["detail"]
        and "1 taken off" in rows["staff-1"]["detail"]
    )
    assert all(row["status"] == "Online" for row in rows.values())
    assert all(row["last_activity"] for k, row in rows.items() if k != "card-printer-1"), "it has printed nothing here"
    assert [row["device"] for row in rep(client, lib, "rfid_clients", kind="gate")["rows"]] == ["gate-1"]
    assert stats(r)["Devices"] == 5 and stats(r)["Offline"] == 0
    client.app.state.devices.get("gate-1", "gate").online = False
    assert (
        next(row for row in rep(client, lib, "rfid_clients")["rows"] if row["device"] == "gate-1")["status"]
        == "Offline"
    )


@pytest.mark.req("AC08", "FR08")
def test_fines_report_adds_up(client, lib, world):
    r = rep(client, lib, "fines")
    s = stats(r)
    assert (s["Charged"], s["Paid"], s["Waived"], s["Opening balances"], s["Owed now (everyone)"]) == (
        0,
        200,
        0,
        1550,
        1350,
    )
    assert r["total"] == 3 and {row["kind"] for row in r["rows"]} == {"Opening balances", "Payments"}
    assert s["Opening balances"] - s["Paid"] == s["Owed now (everyone)"]  # the ledger explains the balances
    only = rep(client, lib, "fines", kind="payment")
    assert only["total"] == 1 and only["rows"][0]["amount"] == 200 and only["rows"][0]["member_no"] == "M0004"
    assert rep(client, lib, "fines", member_no="M0005")["total"] == 1
    assert rep(client, lib, "fines", q="ortiz")["total"] == 2


@pytest.mark.req("AC08", "FR08", "FR07")
def test_gate_events_report(client, lib, world):
    r = rep(client, lib, "gate_events")
    assert r["total"] == 2
    s = stats(r)
    assert (s["Alarms"], s["Unauthorised removals"], s["Repeats"], s["Footfall (people)"]) == (1, 1, 0, 3)
    assert s["Messages waiting or failed"] == 3 and s["Messages sent"] == 0
    alarm = next(row for row in r["rows"] if row["alarm"] == "ALARM")
    assert alarm["accession_no"] == "R1" and alarm["messages"] == "3 queued"
    assert rep(client, lib, "gate_events", alarms_only="1")["total"] == 1
    assert rep(client, lib, "gate_events", kind="authorised")["rows"][0]["accession_no"] == "R2"
    assert rep(client, lib, "gate_events", gate="gate-2")["total"] == 0
    assert [p["value"] for p in r["chart"]["points"]] == [1]  # one alarm today
    client.post("/api/notifications/dispatch", headers=login_card(client, "STAFFADMIN01"))
    assert stats(rep(client, lib, "gate_events"))["Messages sent"] == 3  # the report follows delivery


# ================================================================== CSV export
@pytest.mark.req("AC08", "FR08")
def test_csv_export_matches_the_filters_not_the_page(client, lib, world):
    r = client.get("/api/reports/circulation/export.csv", headers=lib, params={"action": "checkout", "limit": 1})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert (
        r.headers["content-disposition"] == 'attachment; filename="circulation.csv"'
        and r.headers["x-total-rows"] == "3"
    )
    assert r.headers["cache-control"] == "private, no-store"
    lines = r.text.splitlines()
    assert (
        lines[0].startswith("When,Action,Outcome") and len(lines) == 1 + 3
    )  # every matching row, whatever the page size
    fines = client.get("/api/reports/fines/export.csv", headers=lib).text
    assert "12.50" in fines and "2.00" in fines  # money as decimals, not cents


@pytest.mark.req("AC08", "NFR02")
def test_csv_neutralises_spreadsheet_formulas(client, lib):
    item = make_item(client, lib, "EVIL1", '=HYPERLINK("http://x")', location="Shelf-9")
    assert tag(client, lib, item["id"], place(client, lib)).status_code == 201
    body = client.get("/api/reports/tagged_items/export.csv", headers=lib).text
    assert "'=HYPERLINK" in body and ",=HYPERLINK" not in body and "\n=HYPERLINK" not in body


# ================================================================== input checks and access
@pytest.mark.req("AC08", "NFR02")
def test_bad_filters_are_refused_clearly_and_wildcards_do_not_match_everything(client, lib, world):
    def code(report, **q):
        return client.get(f"/api/reports/{report}", headers=lib, params=q)

    assert code("circulation", **{"from": "yesterday"}).status_code == 422
    assert (
        code("circulation", **{"from": "0001-01-01", "to": "9999-12-31"}).status_code == 200
    )  # extremes must not crash
    assert code("circulation", **{"to": "9999-12-31"}).status_code == 200
    assert code("gate_events", **{"from": "0001-01-01"}).status_code == 200
    assert "date like" in code("circulation", **{"from": "07/10/2026"}).json()["detail"]["message"]
    assert (
        "before"
        in code("circulation", **{"from": D, "to": (TODAY - timedelta(days=2)).isoformat()}).json()["detail"]["message"]
    )
    assert code("circulation", action="steal").status_code == 422
    assert code("members", owes="lots").status_code == 422
    assert code("nope").status_code == 404
    assert code("members", state="whatever").status_code == 200  # a filter this report does not have is ignored
    assert (
        rep(client, lib, "circulation", q="%")["total"] == 0 and rep(client, lib, "tagged_items", q="_")["total"] == 0
    )
    assert rep(client, lib, "members", q="%")["total"] == 0 and rep(client, lib, "fines", q="%")["total"] == 0
    assert client.get("/api/reports/circulation/export.csv", headers=lib, params={"outcome": "x"}).status_code == 422


@pytest.mark.req("AC08", "NFR02")
def test_who_may_read_reports(client, lib, clerk):
    anon = TestClient(client.app)
    assert anon.get("/api/reports").status_code == 401
    assert client.get("/api/reports", headers=clerk).status_code == 403  # a desk clerk has no reports.view
    assert client.get("/api/reports/circulation/export.csv", headers=clerk).status_code == 403
    auditor = login_card(client, "STAFFAUDIT01")
    assert client.get("/api/reports/operators", headers=auditor).status_code == 200  # an auditor may
    assert client.get("/reports", headers=clerk).status_code == 403
    page = client.get("/reports", headers=lib).text
    assert (
        "Library reports" in page
        and "Download CSV" in page
        and client.get("/reports", headers=auditor).status_code == 200
    )


# ================================================================== the dashboard
@pytest.mark.req("AC08", "FR08")
def test_dashboard_charts_and_the_most_overdue_loans(client, lib, world, settings):
    age(settings, "R2", days=5)
    d = client.get("/api/reports/charts?days=14", headers=lib).json()
    assert d["days"] == 14 and len(d["circulation"]["points"]) == 14 and len(d["alarms"]["points"]) == 14
    assert d["circulation"]["points"][-1] == {
        "label": D,
        "value": 4,
    }  # four completed transactions today (not the refusal)
    assert d["alarms"]["points"][-1]["value"] == 1
    assert [x["accession_no"] for x in d["overdue"]] == ["R2"] and d["overdue"][0]["days_overdue"] == 5
    assert d["overdue"][0]["fine_so_far_cents"] == 100  # 5 days late, 1 grace day, 0.25 a day
    assert len(client.get("/api/reports/charts?days=500", headers=lib).json()["circulation"]["points"]) == 60  # capped
    page = client.get("/dashboard", headers=lib).text
    assert 'id="chart-circ"' in page and "Most overdue loans" in page
    assert 'id="chart-circ"' not in client.get("/dashboard", headers=login_card(client, "STAFFCLERK01")).text


@pytest.mark.req("AC08")
def test_per_day_fills_quiet_days_and_caps_long_ranges():
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 1, 4, tzinfo=UTC)  # exclusive end: 1, 2 and 3 January
    assert per_day({"2026-01-02": 7}, lo, hi) == [
        {"label": "2026-01-01", "value": 0}, {"label": "2026-01-02", "value": 7}, {"label": "2026-01-03", "value": 0},
    ]  # fmt: skip
    assert [p["label"] for p in per_day({"2026-02-01": 1, "2026-02-03": 2}, None, None)] == [
        "2026-02-01",
        "2026-02-02",
        "2026-02-03",
    ]
    assert per_day({}, None, None) == []
    long = per_day({}, datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert len(long) == MAX_DAYS_CHART and long[-1]["label"] == "2025-12-31"  # the most recent 60 days


@pytest.mark.req("AC08", "NFR06")
def test_reports_change_nothing(client, lib, world, settings):
    before = sql(
        settings,
        COUNTS,
    )
    for r in ("tagged_items", "members", "circulation", "operators", "rfid_clients", "fines", "gate_events"):
        rep(client, lib, r)
        client.get(f"/api/reports/{r}/export.csv", headers=lib)
    assert (
        sql(
            settings,
            COUNTS,
        )
        == before
    )


@pytest.mark.req("AC08", "NFR05")
def test_dashboard_numbers_use_thousands_separators_like_the_rest_of_the_app(client, lib):
    from shelfsense.db import make_engine, make_sessionmaker
    from shelfsense.models import BibRecord, Item

    with make_sessionmaker(make_engine(client.settings.database_url))() as db:  # 1,200 items
        db.bulk_insert_mappings(BibRecord, [{"dedupe_key": f"k{n}", "title": f"T{n}"} for n in range(1200)])
        db.commit()
        ids = [b for (b,) in db.execute(BibRecord.__table__.select().with_only_columns(BibRecord.id))]
        db.bulk_insert_mappings(Item, [{"bib_id": b, "accession_no": f"N{b}"} for b in ids])
        db.commit()
    page = client.get("/dashboard", headers=lib).text
    assert ">1,200<" in page and ">1200<" not in page


@pytest.mark.req("AC08")
def test_the_dashboard_empty_chart_message_is_about_the_dashboard(client, lib):
    page = client.get("/dashboard", headers=lib).text
    assert "No gate alarms in the last" in page and "No completed transactions in the last" in page


@pytest.mark.req("AC08", "FR04")
def test_the_card_printer_appears_in_the_rfid_clients_report_with_its_work(client, lib):
    client.post("/api/circulation/members/M0001/card/personalise", headers=lib)
    client.post("/api/circulation/members/M0002/card/personalise", headers=lib)
    client.app.state.devices.get("card-printer-1", "card_printer").fail_next()
    client.post("/api/circulation/members/M0001/card/personalise", headers=lib)
    row = {r["device"]: r for r in rep(client, lib, "rfid_clients")["rows"]}["card-printer-1"]
    assert (
        row["activity"] == 2 and row["unit"] == "cards printed" and row["detail"] == "1 failed" and row["last_activity"]
    )
    only = rep(client, lib, "rfid_clients", kind="card_printer")["rows"]
    assert [r["device"] for r in only] == ["card-printer-1"]
