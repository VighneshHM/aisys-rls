"""AC05: a handheld mock inventories a shelf; missing / misplaced report; visible and audible confirmation events."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import login_card
from shelfsense.rfid.devices import DeviceError
from shelfsense.rfid.registry import DeviceConfig, DeviceConfigError, DeviceRegistry
from test_tagging import make_item, place, tag

SHELF = "Shelf-1"


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def hh(client):
    return client.app.state.devices.get("handheld-1", "handheld")


def stocked(client, lib, acc, location=SHELF, tagged=True, **extra):
    item = make_item(client, lib, acc, f"Title of {acc}", location=location, **extra)
    uid = None
    if tagged:
        uid = place(client, lib)
        assert tag(client, lib, item["id"], uid).status_code == 201
    return item, uid


@pytest.fixture
def shelves(client, lib):
    """Shelf-1: five tagged books, one untagged book. Shelf-2: two tagged books."""
    for n in range(1, 6):
        stocked(client, lib, f"A{n}")
    stocked(client, lib, "A6", tagged=False)
    for n in range(1, 3):
        stocked(client, lib, f"B{n}", location="Shelf-2")


def begin(client, lib, location=SHELF):
    r = client.post("/api/inventory/sessions", headers=lib, json={"location": location})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def sweep(client, lib, sid):
    r = client.post(f"/api/inventory/sessions/{sid}/scan", headers=lib)
    assert r.status_code == 200, r.text
    return r.json()


def plant(client, lib, sid, **kw):
    r = client.post("/api/inventory/mock/handhelds/handheld-1/load-shelf", headers=lib, json={"session_id": sid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def report(client, lib, sid):
    return client.get(f"/api/inventory/sessions/{sid}", headers=lib).json()


def accs(entries):
    return sorted(e["accession_no"] for e in entries)


# ================================================================== AC05
@pytest.mark.req("AC05", "FR06")
def test_a_shelf_sweep_reports_found_missing_misplaced_and_unknown(client, lib, hh, shelves):
    sid = begin(client, lib)
    planted = plant(client, lib, sid, missing=1, strays=1, unknown=1, duplicates=2)
    swept = sweep(client, lib, sid)
    rep = report(client, lib, sid)

    assert rep["counts"]["expected"] == 5  # tagged and available here: the untagged A6 cannot be read
    assert accs(rep["missing"]) == planted["left_off_the_shelf"] and len(rep["missing"]) == 1
    assert len(rep["found"]) == 4 and all(f["location"] == SHELF for f in rep["found"])
    [stray] = rep["misplaced"]
    assert stray["accession_no"] == planted["strays"][0]["accession_no"] and "Shelf-2" in stray["note"]
    assert len(rep["unknown"]) == 1 and rep["unknown"][0]["accession_no"] is None
    assert rep["counts"]["untagged"] == 1  # reported, not called missing
    assert swept["duplicate_reads"] == 2 and swept["reads"] == swept["distinct"] + 2  # duplicates are coped with
    assert accs(rep["found"] + rep["missing"]) == ["A1", "A2", "A3", "A4", "A5"]


@pytest.mark.req("AC05", "FR06")
def test_every_tag_earns_a_beep_and_a_light_and_it_is_recorded(client, lib, hh, shelves):
    sid = begin(client, lib)
    plant(client, lib, sid, missing=1, strays=1, unknown=1, duplicates=0)
    first = sweep(client, lib, sid)
    assert first["signals"] == {"ok": 4, "warning": 1, "error": 1}
    assert sorted(hh.indications) == ["error", "ok", "ok", "ok", "ok", "warning"]  # what the device did
    rep = report(client, lib, sid)
    assert {e["signal"] for e in rep["found"]} == {"ok"}
    assert [e["signal"] for e in rep["misplaced"]] == ["warning"] and [e["signal"] for e in rep["unknown"]] == ["error"]
    hh.indications.clear()
    second = sweep(client, lib, sid)  # nothing new: no new beeps, but the reads are counted
    assert second["new"] == [] and hh.indications == [] and second["sweep"] == 2
    assert all(e["reads"] == 2 for e in report(client, lib, sid)["found"])


@pytest.mark.req("AC05", "FR06")
def test_a_book_found_on_a_later_sweep_stops_being_missing(client, lib, hh, shelves):
    sid = begin(client, lib)
    planted = plant(client, lib, sid, missing=2, strays=0, unknown=0, duplicates=0)
    sweep(client, lib, sid)
    assert accs(report(client, lib, sid)["missing"]) == planted["left_off_the_shelf"]
    every = [e["tag_uid"] for e in report(client, lib, sid)["found"] + report(client, lib, sid)["missing"]]
    hh.load(every)  # the two books turn up
    sweep(client, lib, sid)
    rep = report(client, lib, sid)
    assert rep["missing"] == [] and len(rep["found"]) == 5


@pytest.mark.req("AC05", "FR04")
def test_loans_are_not_missing_and_a_returned_but_unchecked_book_is_flagged(client, lib, hh, shelves):
    assert (
        client.post("/api/circulation/checkout", headers=lib, json={"member_no": "M0001", "item_ref": "A1"}).status_code
        == 200
    )
    sid = begin(client, lib)
    plant(client, lib, sid, missing=0, strays=0, unknown=0, duplicates=0)
    sweep(client, lib, sid)
    rep = report(client, lib, sid)
    assert rep["counts"]["expected"] == 4 and rep["counts"]["on_loan"] == 1  # A1 is out, so not expected
    assert rep["missing"] == []
    hh.load([e["tag_uid"] for e in rep["found"]] + [_tag_of(client, lib, "A1")])  # ...but it is on the shelf
    sweep(client, lib, sid)
    flagged = report(client, lib, sid)["shelved_on_loan"]
    assert accs(flagged) == ["A1"] and flagged[0]["signal"] == "warning" and "check it in" in flagged[0]["note"]


@pytest.mark.req("AC05", "FR06")
def test_a_written_off_copy_found_on_the_shelf_is_flagged(client, lib, hh, shelves):
    """D-076: a lost or withdrawn copy read on the shelf was reported as plainly found, with nothing to act on."""
    ids = {
        i["accession_no"]: i["id"] for i in client.get("/api/catalogue/items?q=Title of A", headers=lib).json()["items"]
    }
    sid = begin(client, lib)
    plant(client, lib, sid, missing=0, strays=0, unknown=0, duplicates=0)
    for acc, status in (("A1", "lost"), ("A2", "withdrawn"), ("A3", "damaged")):
        r = client.patch(f"/api/catalogue/items/{ids[acc]}", headers=lib, json={"status": status, "reason": "test"})
        assert r.status_code == 200, r.text
    new = {n["accession_no"]: n for n in sweep(client, lib, sid)["new"]}
    assert "set it back to available" in new["A1"]["note"] and "take it off" in new["A2"]["note"]
    assert "damaged" in new["A3"]["note"] and {new[a]["signal"] for a in ("A1", "A2", "A3")} == {"warning"}
    found = {e["accession_no"]: e for e in report(client, lib, sid)["found"]}
    assert found["A4"]["note"] == "" and found["A4"]["signal"] == "ok"
    assert found["A1"]["note"] and found["A2"]["note"] and found["A3"]["note"]


def _tag_of(client, lib, acc):
    item_id = client.get("/api/catalogue/items", headers=lib, params={"q": acc}).json()["items"][0]["id"]
    return client.get(f"/api/catalogue/items/{item_id}", headers=lib).json()["tag"]["uid"]


@pytest.mark.req("AC05", "FR05")
def test_removed_and_retired_tags_are_unknown_on_the_shelf(client, lib, hh, shelves):
    gone, retired = _tag_of(client, lib, "A1"), _tag_of(client, lib, "A2")
    client.post(f"/api/rfid/tags/{gone}/remove", headers=lib, json={})
    client.post(f"/api/rfid/tags/{retired}/retire", headers=lib, json={})
    sid = begin(client, lib)
    hh.load([gone, retired])
    sweep(client, lib, sid)
    notes = {e["tag_uid"]: e["note"] for e in report(client, lib, sid)["unknown"]}
    assert "taken off" in notes[gone] and "Retired" in notes[retired]


@pytest.mark.req("AC05", "FR06")
def test_closing_a_session_freezes_its_report(client, lib, hh, shelves):
    sid = begin(client, lib)
    plant(client, lib, sid, missing=1, strays=0, unknown=0, duplicates=0)
    sweep(client, lib, sid)
    closed = client.post(f"/api/inventory/sessions/{sid}/close", headers=lib).json()
    frozen = closed["counts"]
    assert closed["session"]["status"] == "closed" and frozen["missing"] == 1 and frozen["found"] == 4
    assert client.post(f"/api/inventory/sessions/{sid}/scan", headers=lib).status_code == 409
    assert client.post(f"/api/inventory/sessions/{sid}/close", headers=lib).status_code == 409
    stocked(client, lib, "A9")  # the catalogue changes afterwards...
    assert report(client, lib, sid)["counts"] == frozen  # ...the closed report does not
    again = report(client, lib, sid)
    assert accs(again["missing"]) == accs(closed["missing"]) and len(again["found"]) == 4  # lists are frozen too
    listed = client.get("/api/inventory/sessions", headers=lib).json()
    assert listed[0]["id"] == sid and listed[0]["summary"]["missing"] == 1


@pytest.mark.req("AC05", "NFR06")
def test_handheld_failures_lose_nothing_and_say_why(client, lib, hh, shelves, monkeypatch):
    sid = begin(client, lib)
    plant(client, lib, sid, missing=0, strays=0, unknown=0, duplicates=0)
    hh.online = False
    r = client.post(f"/api/inventory/sessions/{sid}/scan", headers=lib)
    assert r.status_code == 503 and "offline" in r.json()["detail"]["message"]
    hh.online = True
    hh.fail_next("scan")
    assert client.post(f"/api/inventory/sessions/{sid}/scan", headers=lib).status_code == 502
    assert report(client, lib, sid)["session"]["sweeps"] == 0 and report(client, lib, sid)["counts"]["found"] == 0

    def dead_buzzer(pattern):
        raise DeviceError("buzzer broken")

    monkeypatch.setattr(hh, "indicate", dead_buzzer)  # a broken beeper must not lose the scan
    assert sweep(client, lib, sid)["counts"]["found"] == 5


@pytest.mark.req("AC05", "FR08")
def test_the_report_can_be_downloaded_as_csv_safely(client, lib, hh, shelves):
    stocked(client, lib, "FX1")
    evil = make_item(client, lib, "EV1", '=HYPERLINK("http://x")', location=SHELF)
    uid = place(client, lib)
    tag(client, lib, evil["id"], uid)
    sid = begin(client, lib)
    plant(client, lib, sid, missing=0, strays=0, unknown=0, duplicates=0)
    sweep(client, lib, sid)
    r = client.get(f"/api/inventory/sessions/{sid}/export.csv", headers=lib)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="inventory-' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0].startswith("status,accession_no,title")
    assert any(line.startswith("found,A1,") for line in lines)
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text  # no live formula in a spreadsheet


@pytest.mark.req("AC05", "NFR02")
def test_inventory_needs_permission_and_valid_input(client, lib, clerk, shelves):
    anon = TestClient(client.app)
    assert anon.get("/api/inventory/locations").status_code == 401
    assert client.get("/api/inventory/locations", headers=clerk).status_code == 403
    assert client.get("/api/inventory/locations", headers=login_card(client, "STAFFAUDIT01")).status_code == 403
    locs = {loc["location"]: loc for loc in client.get("/api/inventory/locations", headers=lib).json()}
    assert locs[SHELF]["items"] == 6 and locs[SHELF]["tagged"] == 5 and locs["Shelf-2"]["items"] == 2
    bad = client.post("/api/inventory/sessions", headers=lib, json={"location": "Nowhere"})
    assert bad.status_code == 404
    bad = client.post("/api/inventory/sessions", headers=lib, json={"location": SHELF, "handheld_id": "nope"})
    assert bad.status_code == 404
    assert client.get("/api/inventory/sessions/999", headers=lib).status_code == 404
    assert client.get("/inventory", headers=clerk).status_code == 403
    assert "Start a session" in client.get("/inventory", headers=lib).text


@pytest.mark.req("AC05", "FR03", "NFR06")
def test_handhelds_are_configured_like_readers():
    reg = DeviceRegistry((DeviceConfig("hh", "handheld", "mock"),))
    assert reg.describe()[0]["kind"] == "handheld" and reg.describe()[0]["mock"] is True
    assert reg.get("hh", "handheld") is not None and reg.get("hh", "staff_reader") is None
    with pytest.raises(DeviceConfigError, match="unknown driver"):
        DeviceRegistry((DeviceConfig("hh", "handheld", "vendorx"),))
