"""AC02 / FR05 / FR03: catalogue item -> validate -> mock RFID tag -> tag-to-item relationship."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aisys.app import create_app
from aisys.config import load_settings
from aisys.db import make_engine
from aisys.models import RfidTag
from aisys.rfid import tagging
from aisys.rfid.devices import DeviceError, DeviceUnavailable, StaffReader
from aisys.rfid.events import is_valid_tag_uid, normalize_tag_uid
from aisys.rfid.mock import MockStaffReader
from aisys.rfid.registry import DeviceConfig, DeviceConfigError, DeviceRegistry
from conftest import login_card


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")  # librarian: catalogue.write + rfid.tag


@pytest.fixture
def reader(client) -> MockStaffReader:
    return client.app.state.devices.get("staff-1")


def make_item(client, hdr, acc="ACC-1", title="Quantum Algorithms", **extra):
    body = {"title": title, "accession_no": acc, **extra}
    r = client.post("/api/catalogue/items", headers=hdr, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def place(client, hdr, uid=None) -> str:
    r = client.post("/api/rfid/mock/readers/staff-1/tags", headers=hdr, json={"uid": uid})
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def tag(client, hdr, item_id, uid, **extra):
    return client.post("/api/rfid/tagging", headers=hdr, json={"item_id": item_id, "tag_uid": uid, **extra})


def count(settings, table, where="1=1") -> int:
    with make_engine(settings.database_url).connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()


def codes(response) -> set[str]:
    return {p["code"] for p in response.json()["detail"]["problems"]}


# ================================================================== device layer (no database)
@pytest.mark.req("FR03", "AC02")
def test_mock_reader_implements_the_device_neutral_interface():
    r = MockStaffReader("t-1")
    assert isinstance(r, StaffReader)  # a vendor driver only has to satisfy this protocol
    uid = r.place_tag()
    assert is_valid_tag_uid(uid) and uid.startswith("E280")
    [seen] = r.read_field()
    assert (seen.uid, seen.item_ref, seen.security_armed) == (uid, None, False)  # blank tag
    r.encode(uid, "ACC-9")
    [seen] = r.read_field()
    assert (seen.item_ref, seen.security_armed) == ("ACC-9", True)  # identifier written, security bit armed
    r.set_security(uid, False)
    assert r.memory_of(uid).armed is False
    r.clear(uid)
    assert (r.memory_of(uid).item_ref, r.memory_of(uid).armed) == (None, False)
    r.indicate("ok")
    assert r.indications == ["ok"]


@pytest.mark.req("FR03")
def test_mock_reader_failure_modes():
    r = MockStaffReader()
    with pytest.raises(DeviceError, match="not in the reader field"):
        r.encode("E280AAAA11112222", "X")
    uid = r.place_tag()
    r.fail_next("encode")
    with pytest.raises(DeviceError, match="simulated encode failure"):
        r.encode(uid, "X")
    r.encode(uid, "X")  # the injected failure fires once only
    r.online = False
    with pytest.raises(DeviceUnavailable):
        r.read_field()
    assert r.health().online is False


@pytest.mark.req("FR03", "NFR06")
def test_registry_builds_devices_from_config_and_rejects_bad_config():
    reg = DeviceRegistry((DeviceConfig("a"), DeviceConfig("b", driver="mock")))
    assert [d["id"] for d in reg.describe()] == ["a", "b"] and reg.get("a") is not reg.get("b")
    assert reg.get("nope") is None
    for bad, msg in [
        (DeviceConfig("a", driver="vendorx"), "unknown driver"),
        (DeviceConfig("a", kind="toaster"), "unknown kind"),
    ]:
        with pytest.raises(DeviceConfigError, match=msg):
            DeviceRegistry((bad,))
    with pytest.raises(DeviceConfigError, match="duplicate"):
        DeviceRegistry((DeviceConfig("a"), DeviceConfig("a")))


@pytest.mark.req("FR03", "NFR06")
def test_a_new_vendor_driver_plugs_in_without_touching_app_code():
    class VendorReader(MockStaffReader):  # stands in for a real SDK-backed driver
        pass

    class NotAReader:
        kind, device_id = "staff_reader", "x"

        def __init__(self, **_):
            pass

    DeviceRegistry.register_driver("vendorx", VendorReader)
    DeviceRegistry.register_driver("broken", NotAReader)
    try:
        assert isinstance(DeviceRegistry((DeviceConfig("v", driver="vendorx"),)).get("v"), VendorReader)
        with pytest.raises(DeviceConfigError, match="does not implement"):
            DeviceRegistry((DeviceConfig("v", driver="broken"),))
    finally:
        DeviceRegistry._drivers.pop("vendorx", None)
        DeviceRegistry._drivers.pop("broken", None)


@pytest.mark.req("NFR06", "FR03")
def test_devices_are_configured_in_toml_not_in_code(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[devices]]\nid = "desk-7"\nkind = "staff_reader"\ndriver = "mock"\n', encoding="utf-8")
    s = load_settings(cfg)
    assert [d.id for d in s.devices] == ["desk-7"]
    assert [d.id for d in load_settings(tmp_path / "missing.toml").devices] == [
        "staff-1",
        "handheld-1",
    ]  # sensible default


@pytest.mark.req("FR05")
def test_tag_uid_normalisation():
    assert normalize_tag_uid(" e2:80-68 94 0000 5000 ") == "E28068940000500" + "0"
    assert is_valid_tag_uid("E2806894") and not is_valid_tag_uid("E28") and not is_valid_tag_uid("GGGGGGGG")


# ================================================================== catalogue: create + locate
@pytest.mark.req("AC02", "FR01")
def test_create_item_validates_and_reuses_the_bibliographic_record(client, lib):
    a = make_item(client, lib, "A1", "Shared Title", isbn="978-0-306-40615-7", author="Ann")
    b = make_item(client, lib, "A2", "Shared Title", isbn="9780306406157", author="Ann")
    assert a["bib_id"] == b["bib_id"] and a["id"] != b["id"]  # one title, two copies
    assert a["isbn"] == "9780306406157" and a["tag"] is None


@pytest.mark.req("AC02", "FR01")
def test_create_item_rejects_invalid_and_duplicate_records(client, lib):
    bad = client.post(
        "/api/catalogue/items", headers=lib, json={"title": "", "accession_no": "bad id!", "isbn": "123", "price": "x"}
    )
    assert bad.status_code == 422
    assert {p["code"] for p in bad.json()["detail"]["problems"]} >= {
        "required_missing",
        "accession_format",
        "isbn_invalid",
        "price_invalid",
    }
    make_item(client, lib, "D1", barcode="BC-1")
    dup = client.post("/api/catalogue/items", headers=lib, json={"title": "x", "accession_no": "d1"})
    assert dup.status_code == 409 and "accession" in dup.json()["detail"]["message"]  # case-insensitive
    dup = client.post("/api/catalogue/items", headers=lib, json={"title": "x", "accession_no": "D2", "barcode": "bc-1"})
    assert dup.status_code == 409 and "barcode" in dup.json()["detail"]["message"]
    assert (
        client.post(
            "/api/catalogue/items", headers=lib, json={"title": "x", "accession_no": "Z", "hack": 1}
        ).status_code
        == 422
    )


@pytest.mark.req("AC02", "FR02")
def test_locate_items_by_every_identifier(client, lib):
    one = make_item(
        client, lib, "L1", "Rivers of the Coast", author="Liam Novak", isbn="9780306406157", barcode="BC-77"
    )
    make_item(client, lib, "L2", "Other Book", author="Someone Else")
    uid = place(client, lib)
    assert tag(client, lib, one["id"], uid).status_code == 201

    def ids(q, **params):
        r = client.get("/api/catalogue/items", headers=lib, params={"q": q, **params}).json()
        return [i["accession_no"] for i in r["items"]]

    for q in ("rivers", "NOVAK", "978-0-306-40615-7", "9780306406157", "l1", "bc-77", uid, uid.lower()):
        assert ids(q) == ["L1"], q
    assert ids("%") == [] and ids("_") == []  # wildcards are escaped, not interpreted
    assert ids("", tagged="true") == ["L1"] and ids("", tagged="false") == ["L2"]
    assert client.get("/api/catalogue/items", headers=lib).json()["total"] == 2


# ================================================================== AC02: the acceptance scenario
@pytest.mark.req("AC02", "FR05", "FR03")
def test_ac02_create_validate_associate_and_display_the_relationship(client, lib, reader, settings):
    item = make_item(client, lib, "ACC-100", "Quantum Algorithms", author="Asha Rao", isbn="9780306406157")
    uid = place(client, lib)  # a blank tag is put on the (mock) staff reader

    seen = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]
    assert [t["uid"] for t in seen] == [uid] and seen[0]["blank"] is True and seen[0]["known"] is False

    check = client.post("/api/rfid/tagging/validate", headers=lib, json={"item_id": item["id"], "tag_uid": uid}).json()
    assert check["ok"] is True and check["problems"] == [] and check["item"]["accession_no"] == "ACC-100"
    assert count(settings, "rfid_tags") == 0  # validation is a dry run

    r = tag(client, lib, item["id"], uid, reader_id="staff-1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tag"]["uid"] == uid and body["tag"]["status"] == "active" and body["tag"]["security_armed"] is True
    assert body["item"]["title"] == "Quantum Algorithms" and body["indication"] == "ok"

    # the physical tag now carries the accession number and an armed security bit; the reader beeped "ok"
    mem = reader.memory_of(uid)
    assert (mem.item_ref, mem.armed) == ("ACC-100", True) and reader.indications[-1] == "ok"

    # ---- display the relationship, both directions ------------------------------------------------
    by_tag = client.get(f"/api/rfid/tags/{uid.lower()}", headers=lib).json()
    assert by_tag["item"]["id"] == item["id"] and by_tag["item"]["author"] == "Asha Rao"
    assert by_tag["tag"]["uid"] == uid and [h["type"] for h in by_tag["history"]][:2] == ["tagged", "read"]
    by_item = client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()
    assert by_item["tag"]["uid"] == uid and len(by_item["tag_history"]) == 1
    again = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"][0]
    assert (
        again["known"] is True and again["item"]["accession_no"] == "ACC-100" and again["item_ref_on_tag"] == "ACC-100"
    )

    # ---- monitoring + audit ------------------------------------------------------------------------
    events = client.get(f"/api/rfid/events?tag_uid={uid}", headers=lib).json()
    assert {e["type"] for e in events} == {"read", "tagged"}
    aud = login_card(client, "STAFFAUDIT01")
    rows = client.get("/api/audit?action=rfid.tag", headers=aud).json()
    assert rows[0]["actor"] == "librarian" and rows[0]["detail"]["tag_uid"] == uid


@pytest.mark.req("AC02", "AC01", "FR05")
def test_items_imported_from_a_spreadsheet_can_be_located_and_tagged(client, lib):
    csv = b"Acc No,Title,Author\nIMP-1,Imported Book,Imogen Ford\nIMP-2,Another,Bo\n"
    up = client.post("/api/migrations", headers=lib, files={"file": ("a.csv", io.BytesIO(csv), "text/csv")}).json()
    assert client.post(f"/api/migrations/{up['id']}/commit", headers=lib).status_code == 200
    found = client.get("/api/catalogue/items", headers=lib, params={"q": "imported"}).json()["items"]
    assert [i["accession_no"] for i in found] == ["IMP-1"] and found[0]["tag"] is None
    uid = place(client, lib)
    assert tag(client, lib, found[0]["id"], uid).status_code == 201
    assert client.get(f"/api/rfid/tags/{uid}", headers=lib).json()["item"]["author"] == "Imogen Ford"


# ================================================================== validation before tagging (FR05)
@pytest.mark.req("FR05", "AC02")
def test_validation_reports_every_problem_before_anything_is_written(client, lib, settings):
    ok = make_item(client, lib, "V1")
    other = make_item(client, lib, "V2", "Second")
    uid = place(client, lib)
    tag(client, lib, ok["id"], uid)

    def check(item_id, uid_, **kw):
        return client.post("/api/rfid/tagging/validate", headers=lib, json={"item_id": item_id, "tag_uid": uid_, **kw})

    assert check(9999, uid).status_code == 200 and check(9999, uid).json()["problems"][0]["code"] == "item_not_found"
    assert {p["code"] for p in check(other["id"], "xyz").json()["problems"]} == {"tag_uid_invalid"}
    assert {p["code"] for p in check(other["id"], uid).json()["problems"]} == {"tag_bound_elsewhere"}
    assert {p["code"] for p in check(ok["id"], uid).json()["problems"]} == {"tag_already_on_item"}
    fresh = place(client, lib)
    assert {p["code"] for p in check(ok["id"], fresh).json()["problems"]} == {"item_already_tagged"}
    replace = check(ok["id"], fresh, replace=True).json()
    assert replace["ok"] and replace["warnings"][0]["code"] == "will_replace_old_tag"
    assert count(settings, "rfid_tags") == 1  # nothing changed by any of the dry runs


@pytest.mark.req("FR05")
@pytest.mark.parametrize("status", ["lost", "withdrawn"])
def test_lost_or_withdrawn_items_cannot_be_tagged(client, lib, settings, status):
    item = make_item(client, lib, "S1", status=status)
    r = tag(client, lib, item["id"], place(client, lib))
    assert r.status_code == 409 and codes(r) == {"item_status"}
    assert count(settings, "rfid_tags") == 0


@pytest.mark.req("FR05")
def test_tag_conflicts_return_clear_errors(client, lib):
    a, b = make_item(client, lib, "C1"), make_item(client, lib, "C2", "Two")
    uid = place(client, lib)
    assert tag(client, lib, a["id"], uid).status_code == 201
    r = tag(client, lib, b["id"], uid)
    assert (
        r.status_code == 409
        and codes(r) == {"tag_bound_elsewhere"}
        and "C1" in r.json()["detail"]["problems"][0]["message"]
    )
    assert tag(client, lib, a["id"], uid).status_code == 409  # same tag, same item
    assert tag(client, lib, 9999, uid).status_code == 404
    assert tag(client, lib, b["id"], "nothex!").status_code == 422


# ================================================================== re-tagging and retiring
@pytest.mark.req("FR05", "AC02")
def test_retagging_removes_the_old_tag_and_keeps_its_history(client, lib, settings):
    item = make_item(client, lib, "R1")
    old, new = place(client, lib), place(client, lib)
    tag(client, lib, item["id"], old)
    assert tag(client, lib, item["id"], new).status_code == 409  # needs explicit confirmation
    r = tag(client, lib, item["id"], new, replace=True, reason="tag damaged")
    assert (
        r.status_code == 201
        and r.json()["previous_tag"]["uid"] == old
        and r.json()["previous_tag"]["status"] == "removed"
    )
    detail = client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()
    assert detail["tag"]["uid"] == new
    assert [(t["uid"], t["status"]) for t in detail["tag_history"]] == [(old, "removed"), (new, "active")]
    assert detail["tag_history"][0]["end_reason"] == "tag damaged"
    old_view = client.get(f"/api/rfid/tags/{old}", headers=lib).json()
    assert (
        old_view["state"] == "removed" and old_view["tag"]["status"] == "removed" and len(old_view["tags_on_item"]) == 2
    )
    assert {"removed"} <= {h["type"] for h in old_view["history"]}
    assert count(settings, "rfid_tags", "status='active'") == 1


@pytest.mark.req("FR05")
def test_retagging_can_retire_the_old_tag_for_good(client, lib):
    item, other = make_item(client, lib, "R3"), make_item(client, lib, "R4", "Four")
    old, new = place(client, lib), place(client, lib)
    tag(client, lib, item["id"], old)
    check = client.post(
        "/api/rfid/tagging/validate",
        headers=lib,
        json={"item_id": item["id"], "tag_uid": new, "replace": True, "old_tag_fate": "retired"},
    ).json()
    assert check["ok"] and "retired" in check["warnings"][0]["message"]
    r = tag(client, lib, item["id"], new, replace=True, old_tag_fate="retired", reason="damaged")
    assert r.status_code == 201 and r.json()["previous_tag"]["status"] == "retired"
    assert codes(tag(client, lib, other["id"], old)) == {"tag_retired"}  # never again
    assert tag(client, lib, item["id"], place(client, lib), old_tag_fate="shredded").status_code == 422  # unknown fate


@pytest.mark.req("FR05", "NFR01")
def test_database_guarantees_one_active_tag_per_item(client, lib, settings):
    item = make_item(client, lib, "U1")
    tag(client, lib, item["id"], place(client, lib))
    from sqlalchemy.orm import Session

    with Session(make_engine(settings.database_url)) as db:
        db.add(RfidTag(tag_uid="E280DEADBEEF0001", item_id=item["id"], status="active", tagged_by="x"))
        with pytest.raises(IntegrityError):  # even code that bypasses the service cannot create a second active tag
            db.commit()


@pytest.mark.req("FR05")
def test_retire_a_tag(client, lib):
    item = make_item(client, lib, "T1")
    uid = place(client, lib)
    tag(client, lib, item["id"], uid)
    r = client.post(f"/api/rfid/tags/{uid}/retire", headers=lib, json={"reason": "lost on shelf"})
    assert r.status_code == 200 and r.json()["status"] == "retired" and r.json()["security_armed"] is False
    assert client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()["tag"] is None
    assert client.post(f"/api/rfid/tags/{uid}/retire", headers=lib, json={}).status_code == 409
    assert client.post("/api/rfid/tags/E280000000000000/retire", headers=lib, json={}).status_code == 404
    assert client.get("/api/rfid/tags/E280000000000000", headers=lib).status_code == 404


# ================================================================== device failures never leave a half-saved state
@pytest.mark.req("FR05", "FR03", "NFR01")
def test_device_write_failure_saves_nothing_and_signals_error(client, lib, reader, settings):
    item = make_item(client, lib, "F1")
    uid = place(client, lib)
    reader.fail_next("encode")
    r = tag(client, lib, item["id"], uid)
    assert r.status_code == 409 and "nothing was saved" in r.json()["detail"]["message"]
    assert count(settings, "rfid_tags") == 0 and reader.indications[-1] == "error"
    assert reader.memory_of(uid).item_ref is None
    ev = client.get("/api/rfid/events?type=encode_failed", headers=lib).json()
    assert ev and ev[0]["tag_uid"] == uid
    assert tag(client, lib, item["id"], uid).status_code == 201  # the retry works


@pytest.mark.req("FR05", "FR03")
def test_tag_not_on_the_reader_and_reader_offline(client, lib, reader, settings):
    item = make_item(client, lib, "F2")
    r = tag(client, lib, item["id"], "E2800000AAAA0001")  # valid UID, but nobody put it on the pad
    assert r.status_code == 409 and "on the reader" in r.json()["detail"]["message"]
    uid = place(client, lib)
    client.post("/api/rfid/mock/readers/staff-1/power", headers=lib, json={"online": False})
    assert tag(client, lib, item["id"], uid).status_code == 503
    assert client.post("/api/rfid/readers/staff-1/read", headers=lib).status_code == 503
    assert count(settings, "rfid_tags") == 0
    client.post("/api/rfid/mock/readers/staff-1/power", headers=lib, json={"online": True})
    assert tag(client, lib, item["id"], uid).status_code == 201
    assert tag(client, lib, item["id"], uid, reader_id="no-such").status_code == 404


@pytest.mark.req("FR05", "NFR01")
def test_database_failure_after_device_write_undoes_the_device_write(client, lib, reader, settings, monkeypatch):
    item = make_item(client, lib, "F3")
    uid = place(client, lib)

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(tagging.audit, "record", boom)  # fails after the device accepted the write
    with pytest.raises(RuntimeError):
        tag(client, lib, item["id"], uid)
    assert count(settings, "rfid_tags") == 0
    assert reader.memory_of(uid).item_ref is None and reader.memory_of(uid).armed is False  # device rolled back
    assert reader.indications[-1] == "error"


# ================================================================== permissions and environment
@pytest.mark.req("AC02", "AC07", "NFR02")
def test_clerk_can_look_up_but_not_tag_or_create(client, lib, clerk):
    item = make_item(client, lib, "P1")
    uid = place(client, lib)
    tag(client, lib, item["id"], uid)
    assert client.get("/api/catalogue/items", headers=clerk).status_code == 200
    assert client.get(f"/api/rfid/tags/{uid}", headers=clerk).status_code == 200
    assert (
        client.post("/api/catalogue/items", headers=clerk, json={"title": "t", "accession_no": "x"}).status_code == 403
    )
    assert tag(client, clerk, item["id"], uid).status_code == 403
    assert (
        client.post("/api/rfid/tagging/validate", headers=clerk, json={"item_id": 1, "tag_uid": uid}).status_code == 403
    )
    assert client.post("/api/rfid/readers/staff-1/read", headers=clerk).status_code == 403
    assert client.post(f"/api/rfid/tags/{uid}/retire", headers=clerk, json={}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/catalogue/items").status_code == 401
    assert tag(client, {}, item["id"], uid).status_code == 401


@pytest.mark.req("NFR02", "FR03")
def test_simulator_endpoints_do_not_exist_in_production(settings):
    prod = replace(settings, environment="production", secret_key="x" * 40)
    with TestClient(create_app(prod)) as c:
        from aisys.cli import seed_demo

        seed_demo(prod)
        hdr = login_card(c, "STAFFLIB0001")
        assert c.post("/api/rfid/mock/readers/staff-1/tags", headers=hdr, json={}).status_code == 404
        assert c.delete("/api/rfid/mock/readers/staff-1/tags", headers=hdr).status_code == 404
        assert c.get("/api/rfid/readers", headers=hdr).status_code == 200  # real device endpoints still work


@pytest.mark.req("FR03")
def test_list_readers_reports_health(client, lib, reader):
    [r] = [d for d in client.get("/api/rfid/readers", headers=lib).json() if d["kind"] == "staff_reader"]
    assert r == {
        "id": "staff-1",
        "kind": "staff_reader",
        "driver": "MockStaffReader",
        "mock": True,
        "online": True,
        "detail": "mock reader",
    }
    reader.online = False
    assert client.get("/api/rfid/readers", headers=lib).json()[0]["online"] is False


# ================================================================== pages
@pytest.mark.req("AC02", "FR02", "FR11")
def test_catalogue_and_tagging_pages_follow_permissions(client, lib, clerk):
    client.cookies.clear()
    assert client.get("/catalogue", follow_redirects=False).status_code in (302, 307)
    assert client.get("/tagging", follow_redirects=False).status_code in (302, 307)

    client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"})  # clerk: may search, may not tag
    page = client.get("/catalogue")
    assert page.status_code == 200 and "Find or add an item" in page.text
    assert 'id="add-btn"' not in page.text and "/tagging?item=" in page.text and "const CAN_TAG = false" in page.text
    denied = client.get("/tagging")
    assert denied.status_code == 403 and "cannot open this page" in denied.text
    assert 'href="/tagging"' not in client.get("/dashboard").text  # not offered in the sidebar either

    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    cat, tg = client.get("/catalogue"), client.get("/tagging")
    assert 'id="add-btn"' in cat.text and "const CAN_TAG = true" in cat.text
    assert tg.status_code == 200 and "Tag an item" in tg.text and 'id="sim-place"' in tg.text
    for html in (cat.text, tg.text):
        assert "innerHTML" not in html and "{%" not in html and "{{" not in html  # data is inserted as text only
    assert client.get("/static/ui.js").status_code == 200


@pytest.mark.req("NFR02", "FR03")
def test_tagging_page_hides_the_simulator_in_production(settings):
    prod = replace(settings, environment="production", secret_key="x" * 40)
    # production cookies are Secure, so the client must speak https for the session cookie to be sent back
    with TestClient(create_app(prod), base_url="https://testserver") as c:
        from aisys.cli import seed_demo

        seed_demo(prod)
        login_card(c, "STAFFLIB0001")
        html = c.get("/tagging").text
        assert 'id="sim-place"' not in html and "const SIM = false" in html


@pytest.mark.req("AC02", "FR01")
def test_missing_required_fields_get_the_same_messages_as_the_importer(client, lib):
    r = client.post("/api/catalogue/items", headers=lib, json={"author": "Nobody"})  # no title, no accession
    assert r.status_code == 422
    probs = r.json()["detail"]["problems"]
    assert {(p["field"], p["code"]) for p in probs} == {
        ("title", "required_missing"),
        ("accession_no", "required_missing"),
    }
    assert all("required" in p["message"] for p in probs)


# ===== simulator behaves like one reader, not a tag factory =====
@pytest.mark.req("AC02", "FR03")
def test_generating_a_new_tag_replaces_the_tag_on_the_reader(client, lib, reader):
    first = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"replace": True}).json()
    assert first["on_pad"] == 1
    second = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"replace": True}).json()
    assert second["on_pad"] == 1 and second["uid"] != first["uid"]  # overwritten, not accumulated
    assert [t["uid"] for t in client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]] == [
        second["uid"]
    ]
    assert reader.memory_of(first["uid"]) is None  # the earlier tag really left the pad


@pytest.mark.req("FR03")
def test_default_placement_still_adds_so_a_field_can_hold_several_tags(client, lib):
    a = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={}).json()
    b = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={}).json()
    assert b["on_pad"] == 2 and a["uid"] != b["uid"]  # needed for bulk reads (inventory) later


@pytest.mark.req("AC02", "FR05")
def test_a_linked_tag_is_not_offered_once_a_new_tag_is_generated(client, lib):
    one, two = make_item(client, lib, "N1", "Book One"), make_item(client, lib, "N2", "Book Two")
    uid1 = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"replace": True}).json()["uid"]
    assert tag(client, lib, one["id"], uid1).status_code == 201
    uid2 = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"replace": True}).json()["uid"]
    seen = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]
    assert [t["uid"] for t in seen] == [uid2] and seen[0]["blank"] is True  # only the new blank tag is offered
    assert tag(client, lib, two["id"], uid2).status_code == 201
    assert client.get(f"/api/rfid/tags/{uid1}", headers=lib).json()["item"]["accession_no"] == "N1"


# ===== removing a tag from a tagged item =====
def _tagged(client, lib, acc="X1", title="Removable"):
    item = make_item(client, lib, acc, title)
    uid = place(client, lib)
    assert tag(client, lib, item["id"], uid).status_code == 201
    return item, uid


@pytest.mark.req("AC02", "FR05")
def test_removing_a_tag_erases_it_on_the_reader_and_frees_the_item(client, lib, reader, settings):
    item, uid = _tagged(client, lib)
    assert reader.memory_of(uid).armed is True
    r = client.post(f"/api/rfid/tags/{uid}/remove", headers=lib, json={"reason": "wrong item"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "removed" and body["state"] == "removed" and body["security_armed"] is False
    assert body["end_reason"] == "wrong item"
    assert body["device"]["status"] == "cleared" and "erased" in body["device"]["message"]
    mem = reader.memory_of(uid)
    assert (mem.item_ref, mem.armed) == (None, False)  # no leftover identifier or armed security bit
    assert reader.indications[-1] == "ok"
    detail = client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()
    assert detail["tag"] is None and [t["status"] for t in detail["tag_history"]] == ["removed"]
    ev = client.get(f"/api/rfid/events?tag_uid={uid}&type=removed", headers=lib).json()
    assert ev[0]["detail"]["reason"] == "wrong item" and ev[0]["detail"]["device"] == "cleared"
    aud = login_card(client, "STAFFAUDIT01")
    row = client.get("/api/audit?action=rfid.remove", headers=aud).json()[0]
    assert row["actor"] == "librarian" and row["detail"]["device"] == "cleared"
    # the item can be tagged again with a new tag; the removed one stays in its history
    new = place(client, lib)
    assert tag(client, lib, item["id"], new).status_code == 201
    hist = client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()["tag_history"]
    assert [(t["uid"], t["status"]) for t in hist] == [(uid, "removed"), (new, "active")]


@pytest.mark.req("FR05", "FR03", "NFR01")
@pytest.mark.parametrize(
    ("setup", "expected"),
    [("not_on_reader", "not_on_reader"), ("offline", "reader_offline"), ("fail", "device_error")],
)
def test_removal_still_succeeds_when_the_reader_cannot_erase_the_tag(client, lib, reader, setup, expected):
    item, uid = _tagged(client, lib, acc=f"H-{setup}")
    if setup == "not_on_reader":
        reader.remove_tag(uid)  # the book (and its tag) have left the reader
    elif setup == "offline":
        reader.online = False
    else:
        reader.fail_next("clear")
    r = client.post(f"/api/rfid/tags/{uid}/retire", headers=lib, json={})
    assert r.status_code == 200 and r.json()["status"] == "retired"  # the library record is updated regardless
    assert r.json()["device"]["status"] == expected and "physical tag" in r.json()["device"]["message"]
    assert client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()["tag"] is None


@pytest.mark.req("FR05", "NFR02")
def test_only_staff_who_may_tag_can_remove_a_tag(client, lib, clerk):
    item, uid = _tagged(client, lib, acc="PERM-1")
    assert client.post(f"/api/rfid/tags/{uid}/retire", headers=clerk, json={}).status_code == 403
    assert client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()["tag"]["uid"] == uid  # untouched


@pytest.mark.req("AC02")
def test_remove_tag_controls_are_offered_only_to_staff_who_can_tag(client, lib, clerk):
    client.cookies.clear()
    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    assert "endTagPanel" in client.get("/static/ui.js").text
    cat, tg = client.get("/catalogue").text, client.get("/tagging").text
    assert "Remove or retire tag" in cat and "const CAN_TAG = true" in cat and "endTagPanel" in tg
    client.cookies.clear()
    client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"})
    assert "const CAN_TAG = false" in client.get("/catalogue").text  # the script only renders the button when CAN_TAG
