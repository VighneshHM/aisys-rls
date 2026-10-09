"""FR01 cataloguing: correct a record or change a copy's status, by the importer's rules, audited, never deleted."""

from __future__ import annotations

import pytest

from conftest import login_card

pytestmark = pytest.mark.req("FR01")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def make(client, hdr, acc, title="The Lighthouse Ledger", **extra):
    r = client.post("/api/catalogue/items", headers=hdr, json={"title": title, "accession_no": acc, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def patch(client, hdr, item_id, **body):
    return client.patch(f"/api/catalogue/items/{item_id}", headers=hdr, json=body)


def test_a_record_is_corrected_and_the_change_is_audited(client, lib, admin):
    it = make(client, lib, "ED-1", author="Ada Rowe", location="Main/A1")
    r = patch(client, lib, it["id"], author="Ada Rowe-Smith", location="Main/B2", call_number="823 ROW")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["author"], out["location"], out["call_number"]) == ("Ada Rowe-Smith", "Main/B2", "823 ROW")
    assert out["accession_no"] == "ED-1"
    ev = client.get("/api/audit/events?action=catalogue.item.update", headers=admin).json()["events"][0]
    assert ev["detail"]["changes"]["author"] == {"from": "Ada Rowe", "to": "Ada Rowe-Smith"}
    assert set(ev["detail"]["changes"]) == {"author", "location", "call_number"}


def test_a_correction_is_searchable_at_once(client, lib):
    it = make(client, lib, "ED-2", title="A Short Histroy of Quiet")
    patch(client, lib, it["id"], title="A Short History of Quiet")
    hits = client.get("/api/catalogue/items?q=history quiet", headers=lib).json()["items"]
    assert [h["accession_no"] for h in hits] == ["ED-2"]


def test_the_same_rules_as_a_new_item_apply(client, lib):
    it = make(client, lib, "ED-3")
    other = make(client, lib, "ED-4", title="Another Book", barcode="BC-0004")
    assert patch(client, lib, it["id"], title="").status_code == 422
    assert patch(client, lib, it["id"], isbn="9780000000000").status_code == 422  # bad check digit
    assert patch(client, lib, it["id"], item_type="reference").json()["is_reference"] is True  # derived as on import
    clash = patch(client, lib, it["id"], barcode="BC-0004")
    assert clash.status_code == 409 and str(other["id"]) in clash.text
    assert patch(client, lib, it["id"], accession_no="ED-99").status_code == 422, "the accession number is fixed"
    assert patch(client, lib, 999999, title="x").status_code == 404


def test_status_changes_and_their_reason_are_recorded(client, lib, admin):
    it = make(client, lib, "ED-5")
    for status in ("damaged", "lost", "withdrawn", "available"):
        r = patch(client, lib, it["id"], status=status, reason=f"stock check: {status}")
        assert r.status_code == 200 and r.json()["status"] == status, r.text
    assert patch(client, lib, it["id"], status="on_loan").status_code == 422, "loans belong to circulation"
    assert patch(client, lib, it["id"], status="vanished").status_code == 422
    events = client.get("/api/audit/events?action=catalogue.item.update", headers=admin).json()["events"]
    assert [e["detail"]["changes"]["status"]["to"] for e in events][:4] == ["available", "withdrawn", "lost", "damaged"]
    assert events[0]["detail"]["reason"] == "stock check: available"


def test_a_copy_on_loan_keeps_its_status_until_it_is_checked_in(client, lib):
    it = make(client, lib, "ED-6")
    r = client.post("/api/circulation/checkout", headers=lib, json={"member_no": "M0001", "item_ref": "ED-6"})
    assert r.status_code == 200, r.text
    refused = patch(client, lib, it["id"], status="lost")
    assert refused.status_code == 409 and "check it in" in refused.text
    fixed = patch(client, lib, it["id"], location="Main/C3")  # other corrections are fine while it is out
    assert fixed.status_code == 200 and fixed.json()["status"] == "on_loan"


def test_a_damaged_or_withdrawn_copy_cannot_be_lent_and_withdrawn_leaves_the_public_shelf(client, lib):
    it = make(client, lib, "ED-7", location="Main/Z9", call_number="001 AAA")
    patch(client, lib, it["id"], status="withdrawn", reason="worn out")
    r = client.post("/api/circulation/checkout", headers=lib, json={"member_no": "M0001", "item_ref": "ED-7"})
    assert r.status_code != 200 and "withdrawn" in r.text
    shelf = client.get("/api/opac/shelf?location=Main/Z9").json()
    assert "ED-7" not in str(shelf) and "The Lighthouse Ledger" not in str(shelf)
    assert client.get(f"/api/catalogue/items/{it['id']}", headers=lib).status_code == 200, "nothing is deleted"


def test_a_shared_record_correction_names_how_many_copies_it_touches(client, lib, admin):
    a = make(client, lib, "ED-8", title="Shared Title", isbn="9780001000018")
    make(client, lib, "ED-9", title="Shared Title", isbn="9780001000018")
    assert patch(client, lib, a["id"], publisher="Quiet Press").status_code == 200
    ev = client.get("/api/audit/events?action=catalogue.item.update", headers=admin).json()["events"][0]
    assert ev["detail"]["copies_sharing_the_record"] == 2
    b = client.get("/api/catalogue/items?q=ED-9", headers=lib).json()["items"][0]
    assert b["publisher"] == "Quiet Press"


def test_correcting_into_another_records_title_and_isbn_is_refused(client, lib):
    make(client, lib, "ED-10", title="First Book", isbn="9780001000018")
    second = make(client, lib, "ED-11", title="Second Book", isbn="9780001000032")
    r = patch(client, lib, second["id"], title="First Book", isbn="9780001000018")
    assert r.status_code == 409 and "already has this title" in r.text


def test_only_catalogue_writers_edit(client):
    lib_h = login_card(client, "STAFFLIB0001")
    it = make(client, lib_h, "ED-12")
    for card in ("STAFFCLERK01", "STAFFAUDIT01"):
        assert patch(client, login_card(client, card), it["id"], location="X").status_code == 403
    client.cookies.clear()  # no session at all
    assert client.patch(f"/api/catalogue/items/{it['id']}", json={"location": "X"}).status_code == 401


def test_nothing_changed_writes_no_audit_line(client, lib, admin):
    it = make(client, lib, "ED-13", location="Main/A1")
    assert patch(client, lib, it["id"], location="Main/A1").status_code == 200
    assert not client.get("/api/audit/events?action=catalogue.item.update", headers=admin).json()["events"]
