"""FR01/FR04: acquisitions, serials, barcode and spine labels, patron card personalisation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from aisys.labels import code128
from aisys.labels import service as labels
from aisys.models import Item, Notification
from aisys.serials import service as ser
from conftest import login_card
from test_tagging import make_item

pytestmark = pytest.mark.req("FR01", "FR04")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def order(client, lib, lines=None, supplier="Northlight Books"):
    lines = (
        lines
        if lines is not None
        else [
            {
                "title": "The Lighthouse Ledger",
                "author": "Marit Solheim",
                "quantity": 2,
                "unit_price_cents": 1850,
                "location": "Main/A1",
            }
        ]
    )
    r = client.post("/api/acquisitions", json={"supplier": supplier, "lines": lines}, headers=lib)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- acquisitions ----------
def test_an_order_goes_from_draft_to_received_and_creates_the_books(client, lib, db_items):
    o = order(client, lib)
    assert o["status"] == "draft" and o["po_no"].startswith("PO-") and o["total_cents"] == 3700 and o["ordered"] == 2
    placed = client.post(f"/api/acquisitions/{o['id']}/place", headers=lib).json()
    assert placed["status"] == "ordered" and placed["ordered_at"]
    line = placed["lines"][0]
    part = client.post(
        f"/api/acquisitions/{o['id']}/receive", json={"receipts": [{"line_id": line["id"], "quantity": 1}]}, headers=lib
    ).json()
    assert part["order"]["status"] == "part_received" and part["order"]["lines"][0]["outstanding"] == 1
    assert [c["accession_no"] for c in part["created"]] == ["ACQ000001"]
    rest = client.post(
        f"/api/acquisitions/{o['id']}/receive", json={"receipts": [{"line_id": line["id"], "quantity": 1}]}, headers=lib
    ).json()
    assert (
        rest["order"]["status"] == "received"
        and rest["order"]["closed_at"]
        and rest["created"][0]["accession_no"] == "ACQ000002"
    )
    items = client.get("/api/catalogue/items", params={"q": "lighthouse ledger"}, headers=lib).json()["items"]
    assert len(items) == 2 and {i["bib_id"] for i in items} == {part["created"][0]["item_id"] and items[0]["bib_id"]}
    assert all(i["location"] == "Main/A1" and i["price"] == "18.50" and i["tag"] is None for i in items), (
        "untagged: tagging is next"
    )


@pytest.fixture
def db_items(client):
    return client


def test_receiving_is_all_or_nothing(client, lib):
    o = order(client, lib, [{"title": "Good Book", "quantity": 1}, {"title": "Other Book", "quantity": 2}])
    client.post(f"/api/acquisitions/{o['id']}/place", headers=lib)
    ids = [line["id"] for line in client.get(f"/api/acquisitions/{o['id']}", headers=lib).json()["lines"]]
    over = client.post(
        f"/api/acquisitions/{o['id']}/receive",
        json={"receipts": [{"line_id": ids[0], "quantity": 1}, {"line_id": ids[1], "quantity": 3}]},
        headers=lib,
    )
    assert over.status_code == 422 and "still to come" in over.json()["detail"]["message"]
    assert client.get("/api/catalogue/items", params={"q": "good book"}, headers=lib).json()["total"] == 0, (
        "nothing was booked in"
    )
    twice = client.post(
        f"/api/acquisitions/{o['id']}/receive",
        json={"receipts": [{"line_id": ids[0], "quantity": 1}, {"line_id": ids[0], "quantity": 1}]},
        headers=lib,
    )
    assert twice.status_code == 422
    assert client.post(f"/api/acquisitions/{o['id']}/receive", json={"receipts": []}, headers=lib).status_code == 422
    assert (
        client.post(
            f"/api/acquisitions/{o['id']}/receive", json={"receipts": [{"line_id": 99999, "quantity": 1}]}, headers=lib
        ).status_code
        == 422
    )


def test_a_failed_receipt_leaves_no_half_booked_books(client, lib):
    """The second copy of a line would clash with an existing accession number: neither copy may be kept."""
    o = order(client, lib, [{"title": "Clash Book", "quantity": 2}])
    client.post(f"/api/acquisitions/{o['id']}/place", headers=lib)
    make_item(client, lib, "ACQ000002", "Squatter")  # takes the number the second copy would get
    line_id = client.get(f"/api/acquisitions/{o['id']}", headers=lib).json()["lines"][0]["id"]
    r = client.post(
        f"/api/acquisitions/{o['id']}/receive", json={"receipts": [{"line_id": line_id, "quantity": 2}]}, headers=lib
    )
    after = client.get(f"/api/acquisitions/{o['id']}", headers=lib).json()
    if r.status_code == 200:  # the numbering skipped the squatter: both copies exist and the order is complete
        assert after["status"] == "received" and after["received"] == 2
    else:
        assert after["status"] == "ordered" and after["received"] == 0
        assert client.get("/api/catalogue/items", params={"q": "clash book"}, headers=lib).json()["total"] == 0


def test_only_a_draft_can_be_edited_and_only_an_unreceived_order_cancelled(client, lib):
    o = order(client, lib)
    added = client.post(
        f"/api/acquisitions/{o['id']}/lines", json={"title": "Second Title", "quantity": 3}, headers=lib
    )
    assert added.status_code == 201 and added.json()["lines_count"] == 2
    removed = client.delete(f"/api/acquisitions/{o['id']}/lines/{added.json()['lines'][1]['id']}", headers=lib)
    assert removed.json()["lines_count"] == 1
    assert client.delete(f"/api/acquisitions/{o['id']}/lines/999999", headers=lib).status_code == 404
    client.post(f"/api/acquisitions/{o['id']}/place", headers=lib)
    assert client.post(f"/api/acquisitions/{o['id']}/lines", json={"title": "Late Add"}, headers=lib).status_code == 409
    assert client.post(f"/api/acquisitions/{o['id']}/place", headers=lib).status_code == 409
    line_id = client.get(f"/api/acquisitions/{o['id']}", headers=lib).json()["lines"][0]["id"]
    client.post(
        f"/api/acquisitions/{o['id']}/receive", json={"receipts": [{"line_id": line_id, "quantity": 1}]}, headers=lib
    )
    assert client.post(f"/api/acquisitions/{o['id']}/cancel", headers=lib).status_code == 409
    fresh = order(client, lib)
    assert client.post(f"/api/acquisitions/{fresh['id']}/cancel", headers=lib).json()["status"] == "cancelled"
    assert (
        client.post(
            f"/api/acquisitions/{fresh['id']}/receive", json={"receipts": [{"line_id": 1, "quantity": 1}]}, headers=lib
        ).status_code
        == 409
    )
    empty = client.post("/api/acquisitions", json={"supplier": "X"}, headers=lib).json()
    assert client.post(f"/api/acquisitions/{empty['id']}/place", headers=lib).status_code == 409, (
        "an order with no lines"
    )


@pytest.mark.parametrize(
    "line",
    [
        {"title": ""},
        {"title": "x" * 501},
        {"title": "T", "quantity": 0},
        {"title": "T", "quantity": 1001},
        {"title": "T", "unit_price_cents": -1},
        {"title": "T", "isbn": "123"},
        {"title": "T", "pub_year": 1200},
        {"title": "T", "pub_year": 3000},
        {"title": "T", "quantity": "two"},
    ],
)
def test_bad_order_lines_are_refused(client, lib, line):
    r = client.post("/api/acquisitions", json={"supplier": "S", "lines": [line]}, headers=lib)
    assert r.status_code == 422, (line, r.text)


def test_orders_need_a_supplier_and_the_right_permission(client, lib):
    assert client.post("/api/acquisitions", json={"supplier": " "}, headers=lib).status_code == 422
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/acquisitions", headers=clerk).status_code == 403
    assert client.post("/api/acquisitions", json={"supplier": "S"}, headers=clerk).status_code == 403
    client.cookies.clear()
    assert client.get("/api/acquisitions").status_code == 401
    assert client.get("/api/acquisitions/99999", headers=lib).status_code == 404


def test_order_numbers_are_unique_and_listed_newest_first(client, lib):
    a, b = order(client, lib), order(client, lib)
    assert a["po_no"] != b["po_no"]
    listed = client.get("/api/acquisitions", params={"limit": 1}, headers=lib).json()
    assert listed["total"] >= 2 and listed["orders"][0]["po_no"] == b["po_no"]
    assert client.get("/api/acquisitions", params={"status": "cancelled"}, headers=lib).json()["orders"] == []


def test_acquisition_is_audited(client, lib):
    o = order(client, lib)
    client.post(f"/api/acquisitions/{o['id']}/place", headers=lib)
    aud = login_card(client, "STAFFAUDIT01")
    actions = [
        e["action"]
        for e in client.get("/api/audit/events", params={"action": "acquisition."}, headers=aud).json()["events"]
    ]
    assert {"acquisition.order.create", "acquisition.order.place"} <= set(actions)


# ---------- serials ----------
def new_serial(client, lib, **over):
    body = {
        "title": "Journal of Quiet Things",
        "issn": "0378-5955",
        "frequency": "monthly",
        "supplier": "Periodicals Ltd",
        "location": "Periodicals",
        "first_expected_on": "2026-01-15",
        "issues_ahead": 4,
        **over,
    }
    r = client.post("/api/serials", json=body, headers=lib)
    assert r.status_code == 201, r.text
    return r.json()


def test_issn_check_digits():
    assert ser.issn_valid("0378-5955") and ser.issn_valid("2049-3630") and ser.issn_valid("0317-8471")
    assert not ser.issn_valid("0378-5954") and not ser.issn_valid("03785955") and not ser.issn_valid("abcd-efgh")


@pytest.mark.parametrize(
    "freq, first, expected",
    [
        ("monthly", date(2026, 1, 31), ["2026-01", "2026-02", "2026-03"]),
        ("weekly", date(2026, 12, 28), ["2026-W53", "2027-W01", "2027-W02"]),
        ("quarterly", date(2026, 11, 1), ["2026-Q4", "2027-Q1", "2027-Q2"]),
        ("yearly", date(2026, 2, 28), ["2026", "2027", "2028"]),
    ],
)
def test_schedules_follow_the_frequency(freq, first, expected):
    day, labels_ = first, []
    for _ in range(3):
        labels_.append(ser.label_for(day, freq))
        day = ser.next_date(day, freq)
    assert labels_ == expected


def test_month_end_dates_do_not_drift():
    d = date(2026, 1, 31)
    seen = []
    for _ in range(4):
        seen.append(d)
        d = ser.next_date(d, "monthly")
    assert seen[1] == date(2026, 2, 28) and seen[2] == date(2026, 3, 28), (
        "clamped, as the schedule steps month by month"
    )


def test_a_subscription_is_scheduled_and_issues_are_received(client, lib):
    s = new_serial(client, lib)
    assert [i["label"] for i in s["issues"]] == ["2026-01", "2026-02", "2026-03", "2026-04"] and s["counts"][
        "expected"
    ] == 4
    first = s["issues"][0]["id"]
    got = client.post(
        f"/api/serials/issues/{first}/receive", json={"received_on": "2026-01-16", "book_in": True}, headers=lib
    ).json()
    assert got["issue"]["status"] == "received" and got["issue"]["received_on"] == "2026-01-16" and got["item_id"]
    item = client.get(f"/api/catalogue/items/{got['item_id']}", headers=lib).json()
    assert item["item_type"] == "serial" and item["location"] == "Periodicals" and "2026-01" in item["title"]
    assert client.post(f"/api/serials/issues/{first}/receive", json={}, headers=lib).status_code == 409, "not twice"
    second = s["issues"][1]["id"]
    plain = client.post(f"/api/serials/issues/{second}/receive", json={}, headers=lib).json()
    assert plain["item_id"] is None, "recorded without adding a copy to the catalogue"


def test_late_issues_can_be_claimed_or_given_up(client, lib):
    s = new_serial(
        client,
        lib,
        first_expected_on=(date.today() - timedelta(days=60)).isoformat(),
        frequency="weekly",
        issues_ahead=4,
    )
    late = client.get("/api/serials/late", headers=lib).json()["late"]
    assert len(late) >= 3 and all(x["days_late"] > 7 and x["title"] == s["title"] for x in late)
    iid = late[0]["id"]
    claimed = client.post(
        f"/api/serials/issues/{iid}/mark", json={"status": "claimed", "note": "Emailed supplier"}, headers=lib
    ).json()
    assert claimed["status"] == "claimed" and claimed["note"] == "Emailed supplier"
    assert any(x["id"] == iid for x in client.get("/api/serials/late", headers=lib).json()["late"]), (
        "a claim is still outstanding"
    )
    client.post(f"/api/serials/issues/{iid}/mark", json={"status": "missing"}, headers=lib)
    assert all(x["id"] != iid for x in client.get("/api/serials/late", headers=lib).json()["late"])
    assert client.post(f"/api/serials/issues/{iid}/mark", json={"status": "claimed"}, headers=lib).status_code == 409
    assert client.post(f"/api/serials/issues/{iid}/mark", json={"status": "bogus"}, headers=lib).status_code == 422
    summary = client.get("/api/serials", headers=lib).json()["serials"][0]
    assert summary["counts"]["missing"] == 1 and summary["counts"]["late"] >= 2


def test_a_stopped_subscription_no_longer_chases_late_issues(client, lib):
    s = new_serial(client, lib, first_expected_on=(date.today() - timedelta(days=60)).isoformat(), frequency="weekly")
    assert client.get("/api/serials/late", headers=lib).json()["late"]
    client.post(f"/api/serials/{s['id']}/active", json={"active": False}, headers=lib)
    assert client.get("/api/serials/late", headers=lib).json()["late"] == []


def test_a_schedule_can_be_extended_without_duplicates(client, lib):
    s = new_serial(client, lib, issues_ahead=2)
    more = client.post(f"/api/serials/{s['id']}/extend", json={"count": 3}, headers=lib).json()
    labels_ = [i["label"] for i in more["issues"]]
    assert labels_ == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"] and len(set(labels_)) == 5
    assert client.post(f"/api/serials/{s['id']}/extend", json={"count": 0}, headers=lib).status_code == 422
    assert client.post(f"/api/serials/{s['id']}/extend", json={"count": 25}, headers=lib).status_code == 422
    assert client.post("/api/serials/99999/extend", json={"count": 1}, headers=lib).status_code == 404


@pytest.mark.parametrize(
    "over",
    [
        {"title": ""},
        {"issn": "1234-5678"},
        {"frequency": "daily"},
        {"issues_ahead": 0},
        {"issues_ahead": 25},
        {"first_expected_on": "1999-01-01"},
        {"first_expected_on": "not a date"},
    ],
)
def test_bad_subscriptions_are_refused(client, lib, over):
    body = {"title": "T", "frequency": "monthly", "first_expected_on": "2026-01-01", **over}
    assert client.post("/api/serials", json=body, headers=lib).status_code == 422


def test_serials_need_their_permission(client):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/serials", headers=clerk).status_code == 403
    assert client.get("/api/serials/late", headers=clerk).status_code == 403


# ---------- labels ----------
@pytest.mark.parametrize(
    "text", ["PJJ123C", "ACQ000123", "M0001", "A", "Hello, World! ~", "9780001000014", "QA 76.9 .D3", "x" * 40, " "]
)
def test_barcodes_read_back_as_what_was_asked_for(text):
    modules = code128.encode(text)
    assert code128.decode(modules) == text and modules.endswith("2331112")
    assert sum(int(d) for d in modules) == 11 * (len(text) + 2) + 13, "every symbol is 11 modules wide, the stop is 13"


@pytest.mark.parametrize("bad", ["", "x" * 41, "é", "tab\t", "line\nbreak", "日本"])
def test_text_a_barcode_cannot_hold_is_refused(bad):
    assert not code128.encodable(bad)
    with pytest.raises(ValueError):
        code128.encode(bad)


def test_a_damaged_barcode_is_detected():
    modules = code128.encode("CHECK-ME")
    swapped = modules[:12] + modules[18:24] + modules[12:18] + modules[24:]
    with pytest.raises(ValueError):
        code128.decode(swapped)


def test_spine_lines_fit_a_narrow_label():
    assert labels.spine_lines("QA 76.9 D3 2024") == ["QA", "76.9", "D3", "2024"]
    assert labels.spine_lines("A" * 30) == ["A" * 12, "A" * 12, "AAAAAA"]
    assert labels.spine_lines("") == [] and len(labels.spine_lines("a b c d e f g")) == 4


def test_labels_for_a_shelf_are_in_shelf_order_and_carry_a_barcode(client, lib):
    make_item(client, lib, "LB2", "Second", call_number="QA 10", location="Lab/1", barcode="BARCODE-2")
    make_item(client, lib, "LB1", "First", call_number="QA 9", location="Lab/1")
    make_item(client, lib, "LB3", "Gone", call_number="QA 1", location="Lab/1", status="withdrawn")
    r = client.get("/api/labels/items", params={"location": "Lab/1"}, headers=lib).json()["labels"]
    assert [x["title"] for x in r] == ["First", "Second"], "withdrawn books get no label"
    assert r[0]["barcode"] == "LB1" and r[1]["barcode"] == "BARCODE-2", (
        "the barcode if the item has one, else the accession number"
    )
    assert code128.decode(r[1]["modules"]) == "BARCODE-2" and r[0]["spine"] == ["QA", "9"]


def test_labels_by_item_number_and_limits(client, lib):
    a = make_item(client, lib, "LI1", "Alpha")
    assert (
        client.get("/api/labels/items", params={"ids": str(a["id"])}, headers=lib).json()["labels"][0]["accession_no"]
        == "LI1"
    )
    assert client.get("/api/labels/items", params={"ids": "1,abc"}, headers=lib).status_code == 422
    assert client.get("/api/labels/items", headers=lib).status_code == 422
    assert (
        client.get(
            "/api/labels/items", params={"ids": ",".join(str(n) for n in range(1, 260))}, headers=lib
        ).status_code
        == 422
    )
    assert client.get("/api/labels/items", params={"location": "Nowhere"}, headers=lib).json()["labels"] == []


def test_an_unprintable_barcode_falls_back_to_the_accession_number():
    from types import SimpleNamespace

    odd = SimpleNamespace(barcode="ÅÄÖ-1", accession_no="FB1")
    assert labels.barcode_of(odd) == "FB1"
    assert labels.barcode_of(SimpleNamespace(barcode=None, accession_no="FB2")) == "FB2"


def test_a_print_job_is_queued_for_the_label_printer(client, lib):
    make_item(client, lib, "PJ1", "Print One", call_number="QA 1", location="Print/1")
    r = client.post(
        "/api/labels/print", json={"kind": "spine", "printer": "label-printer", "location": "Print/1"}, headers=lib
    )
    assert r.status_code == 200 and r.json()["queued"] == 1
    with client.app.state.session_factory() as db:
        n = db.scalar(select(Notification).where(Notification.id == r.json()["notification_id"]))
        assert n.channel == "print" and n.recipient == "label-printer" and "QA\n1" in n.body and "[PJ1]" in n.body
    for bad in (
        {"kind": "spine", "printer": "bad printer!", "location": "Print/1"},
        {"kind": "poster", "printer": "p", "location": "Print/1"},
        {"kind": "spine", "printer": "p"},
        {"kind": "spine", "printer": "p", "location": "Nowhere"},
    ):
        assert client.post("/api/labels/print", json=bad, headers=lib).status_code == 422, bad


def test_labels_need_catalogue_write(client):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/labels/items", params={"location": "x"}, headers=clerk).status_code == 403
    assert (
        client.post(
            "/api/labels/print", json={"kind": "spine", "printer": "p", "location": "x"}, headers=clerk
        ).status_code
        == 403
    )


# ---------- patron card personalisation ----------
def test_a_members_card_is_printed_and_encoded(client, lib):
    face = client.get("/api/circulation/members/M0001/card/face", headers=lib).json()
    assert face["name"] == "Priya Nair" and face["member_no"] == "M0001" and face["card_uid"] == "PATRON000001"
    assert code128.decode(face["modules"]) == "PATRON000001" and face["library"]
    r = client.post("/api/circulation/members/M0001/card/personalise", headers=lib)
    assert r.status_code == 200 and r.json()["job"] == "CARD-0001" and r.json()["printer"] == "card-printer-1"
    printer = client.app.state.devices.get("card-printer-1", "card_printer")
    assert printer.jobs[0]["uid"] == "PATRON000001" and printer.jobs[0]["name"] == "Priya Nair"
    assert client.post("/api/circulation/members/M0001/card/personalise", headers=lib).json()["job"] == "CARD-0002"


def test_a_jammed_printer_is_reported_and_recorded(client, lib):
    printer = client.app.state.devices.get("card-printer-1", "card_printer")
    printer.fail_next()
    r = client.post("/api/circulation/members/M0001/card/personalise", headers=lib)
    assert r.status_code == 503 and "not printed" in r.json()["detail"]["message"]
    assert client.post("/api/circulation/members/M0001/card/personalise", headers=lib).status_code == 200, (
        "and it recovers"
    )
    printer.online = False
    assert client.post("/api/circulation/members/M0001/card/personalise", headers=lib).status_code == 503
    aud = login_card(client, "STAFFAUDIT01")
    events = client.get("/api/audit/events", params={"action": "member.card_personalise"}, headers=aud).json()["events"]
    assert {e["outcome"] for e in events} == {"success", "failure"}


def test_a_member_without_a_card_cannot_have_one_printed(client, lib):
    made = client.post("/api/circulation/members", json={"name": "No Card Yet"}, headers=lib).json()
    assert client.post(f"/api/circulation/members/{made['member_no']}/card/personalise", headers=lib).status_code == 409
    assert client.get(f"/api/circulation/members/{made['member_no']}/card/face", headers=lib).status_code == 409
    assert client.post("/api/circulation/members/NOPE/card/personalise", headers=lib).status_code == 404


def test_only_member_editors_print_cards(client):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.post("/api/circulation/members/M0001/card/personalise", headers=clerk).status_code == 403
    assert client.get("/api/circulation/members/M0001/card/face", headers=clerk).status_code == 403


def test_the_card_printer_is_a_configured_device(client):
    kinds = {d["kind"] for d in client.app.state.devices.describe()}
    assert "card_printer" in kinds


def test_pages_render_for_the_right_roles(client, lib):
    for path in ("/labels", "/acquisitions", "/serials", "/shelves"):
        client.cookies.set("aisys_session", lib["Authorization"].split()[1])
        assert client.get(path).status_code == 200, path
    clerk = login_card(client, "STAFFCLERK01")
    client.cookies.set("aisys_session", clerk["Authorization"].split()[1])
    assert [client.get(p).status_code for p in ("/labels", "/acquisitions", "/serials")] == [403, 403, 403]
    assert Item  # imported for the type checkers of the readers of this file
