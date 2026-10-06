"""Tag lifecycle: removed (reusable) vs retired (permanent), the tag registry, search by old tags, migration 0004."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aisys import migrate
from aisys.db import make_engine
from aisys.models import RfidTag
from conftest import login_card
from test_tagging import codes, count, make_item, place, tag


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def reader(client):
    return client.app.state.devices.get("staff-1")


def tagged(client, lib, acc, title="Some Title"):
    item = make_item(client, lib, acc, title)
    uid = place(client, lib)
    assert tag(client, lib, item["id"], uid).status_code == 201
    return item, uid


def end(client, lib, uid, how, reason="", **kw):
    return client.post(f"/api/rfid/tags/{uid}/{how}", headers=lib, json={"reason": reason, **kw})


# ================================================================== removed = reusable
@pytest.mark.req("FR05", "AC02")
def test_a_removed_tag_can_be_linked_again_to_another_item(client, lib, reader):
    first, uid = tagged(client, lib, "L1", "First Book")
    second = make_item(client, lib, "L2", "Second Book")
    assert end(client, lib, uid, "remove", "wrong item").json()["state"] == "removed"

    check = client.post(
        "/api/rfid/tagging/validate", headers=lib, json={"item_id": second["id"], "tag_uid": uid}
    ).json()
    assert check["ok"] and check["warnings"][0]["code"] == "tag_reused" and "L1" in check["warnings"][0]["message"]

    r = tag(client, lib, second["id"], uid)
    assert r.status_code == 201 and r.json()["reused_tag"] is True
    assert reader.memory_of(uid).item_ref == "L2"  # the reader rewrote the tag for its new item
    view = client.get(f"/api/rfid/tags/{uid}", headers=lib).json()
    assert view["state"] == "active" and view["item"]["accession_no"] == "L2"
    assert [(a["item"]["accession_no"], a["status"]) for a in view["associations"]] == [
        ("L1", "removed"),
        ("L2", "active"),
    ]
    old = client.get(f"/api/catalogue/items/{first['id']}", headers=lib).json()
    assert old["tag"] is None and [t["status"] for t in old["tag_history"]] == [
        "removed"
    ]  # first item keeps its history
    ev = client.get(f"/api/rfid/events?tag_uid={uid}&type=tagged", headers=lib).json()
    assert ev[0]["detail"]["reused_tag"] is True


@pytest.mark.req("FR05")
def test_a_removed_tag_on_the_reader_is_offered_for_reuse(client, lib):
    _, uid = tagged(client, lib, "L3")
    end(client, lib, uid, "remove")
    client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"uid": uid, "replace": True})
    [seen] = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]
    assert seen["state"] == "removed" and seen["usable"] is True and seen["blank"] is True  # erased by the removal


@pytest.mark.req("FR05", "NFR01")
def test_the_database_allows_one_active_item_per_tag(client, lib, settings):
    _, uid = tagged(client, lib, "L4")
    other = make_item(client, lib, "L5", "Other")
    with Session(make_engine(settings.database_url)) as db:
        db.add(RfidTag(tag_uid=uid, item_id=other["id"], status="active", tagged_by="x"))
        with pytest.raises(IntegrityError):  # even code that bypasses the service cannot put one tag on two items
            db.commit()
    with Session(make_engine(settings.database_url)) as db:  # ...but ended links may repeat freely
        db.add(RfidTag(tag_uid=uid, item_id=other["id"], status="removed", tagged_by="x"))
        db.commit()


# ================================================================== retired = permanent
@pytest.mark.req("FR05", "AC02")
def test_a_retired_tag_can_never_be_linked_again(client, lib, reader):
    item, uid = tagged(client, lib, "P1")
    other = make_item(client, lib, "P2", "Other")
    r = end(client, lib, uid, "retire", "damaged")
    assert r.status_code == 200 and r.json()["state"] == "retired" and r.json()["device"]["status"] == "cleared"
    assert codes(tag(client, lib, other["id"], uid)) == {"tag_retired"}
    assert codes(tag(client, lib, item["id"], uid)) == {"tag_retired"}
    client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"uid": uid, "replace": True})
    [seen] = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]
    assert seen["state"] == "retired" and seen["usable"] is False
    assert end(client, lib, uid, "retire").status_code == 409  # already retired
    assert end(client, lib, uid, "remove").status_code == 409


@pytest.mark.req("FR05")
def test_a_removed_tag_found_damaged_can_be_retired_afterwards(client, lib):
    _, uid = tagged(client, lib, "P3")
    other = make_item(client, lib, "P4", "Other")
    end(client, lib, uid, "remove", "wrong item")
    r = end(client, lib, uid, "retire", "cracked antenna")
    assert r.status_code == 200 and r.json()["device"]["status"] == "not_linked"
    assert "wrong item" in r.json()["end_reason"] and "cracked antenna" in r.json()["end_reason"]  # both reasons kept
    assert codes(tag(client, lib, other["id"], uid)) == {"tag_retired"}
    assert client.get(f"/api/rfid/tags/{uid}", headers=lib).json()["state"] == "retired"


@pytest.mark.req("FR05")
def test_ending_a_tag_that_cannot_be_ended_is_refused_clearly(client, lib):
    _, uid = tagged(client, lib, "E1")
    end(client, lib, uid, "remove")
    assert end(client, lib, uid, "remove").status_code == 409  # already off an item
    for how in ("remove", "retire"):
        r = end(client, lib, "E280000000000001", how)  # never linked to anything
        assert r.status_code == 404 and "never been linked" in r.json()["detail"]["message"]


@pytest.mark.req("FR05", "AC07", "NFR02")
def test_only_staff_who_may_tag_can_remove_or_retire(client, lib):
    _, uid = tagged(client, lib, "A1")
    clerk = login_card(client, "STAFFCLERK01")
    assert end(client, clerk, uid, "remove").status_code == 403
    assert end(client, clerk, uid, "retire").status_code == 403
    assert client.get(f"/api/rfid/tags/{uid}", headers=lib).json()["state"] == "active"


@pytest.mark.req("FR05", "FR03", "NFR01")
@pytest.mark.parametrize(
    ("setup", "expected"),
    [("not_on_reader", "not_on_reader"), ("offline", "reader_offline"), ("fail", "device_error")],
)
@pytest.mark.parametrize("how", ["remove", "retire"])
def test_ending_a_tag_never_depends_on_the_reader(client, lib, reader, how, setup, expected):
    item, uid = tagged(client, lib, f"D-{how}-{setup}")
    if setup == "not_on_reader":
        reader.remove_tag(uid)
    elif setup == "offline":
        reader.online = False
    else:
        reader.fail_next("clear")
    r = end(client, lib, uid, how)
    assert r.status_code == 200 and r.json()["status"] == ("removed" if how == "remove" else "retired")
    assert r.json()["device"]["status"] == expected and "physical tag" in r.json()["device"]["message"]
    assert client.get(f"/api/catalogue/items/{item['id']}", headers=lib).json()["tag"] is None


@pytest.mark.req("FR05")
def test_removing_and_retiring_are_audited_separately(client, lib):
    _, u1 = tagged(client, lib, "AU1")
    _, u2 = tagged(client, lib, "AU2")
    end(client, lib, u1, "remove", "mistake")
    end(client, lib, u2, "retire", "damaged")
    aud = login_card(client, "STAFFAUDIT01")
    assert client.get("/api/audit?action=rfid.remove", headers=aud).json()[0]["detail"]["tag_uid"] == u1
    assert client.get("/api/audit?action=rfid.retire", headers=aud).json()[0]["detail"]["tag_uid"] == u2
    types = {e["type"] for e in client.get("/api/rfid/events?limit=100", headers=lib).json()}
    assert {"removed", "retired", "tagged"} <= types


# ================================================================== the tag registry
@pytest.fixture
def mixed_tags(client, lib):
    """Three tags in three states: A active, B removed, C retired; B was reused once (two links)."""
    a_item, a = tagged(client, lib, "REG-A", "Alpha Atlas")
    b_item, b = tagged(client, lib, "REG-B", "Beta Botany")
    end(client, lib, b, "remove", "wrong item")
    b2_item = make_item(client, lib, "REG-B2", "Beta Again")
    tag(client, lib, b2_item["id"], b)
    end(client, lib, b, "remove", "second thoughts")
    c_item, c = tagged(client, lib, "REG-C", "Gamma Geology")
    end(client, lib, c, "retire", "damaged")
    return {"a": a, "b": b, "c": c}


@pytest.mark.req("FR05", "FR08")
def test_registry_lists_every_tag_with_its_state(client, lib, mixed_tags):
    data = client.get("/api/rfid/tags", headers=lib).json()
    assert data["total"] == 3 and data["counts"] == {"active": 1, "removed": 1, "retired": 1}
    by_uid = {t["uid"]: t for t in data["tags"]}
    assert by_uid[mixed_tags["a"]]["state"] == "active" and by_uid[mixed_tags["a"]]["links"] == 1
    assert by_uid[mixed_tags["b"]]["state"] == "removed" and by_uid[mixed_tags["b"]]["links"] == 2  # used twice
    assert by_uid[mixed_tags["b"]]["item"]["accession_no"] == "REG-B2"  # the item it was last on
    assert by_uid[mixed_tags["b"]]["end_reason"] == "second thoughts"
    assert by_uid[mixed_tags["c"]]["state"] == "retired" and by_uid[mixed_tags["c"]]["end_reason"] == "damaged"


@pytest.mark.req("FR05", "FR08")
def test_registry_filters_by_state_and_searches_by_uid_item_or_title(client, lib, mixed_tags):
    def uids(**params):
        return {t["uid"] for t in client.get("/api/rfid/tags", headers=lib, params=params).json()["tags"]}

    assert uids(state="active") == {mixed_tags["a"]}
    assert uids(state="removed") == {mixed_tags["b"]}
    assert uids(state="retired") == {mixed_tags["c"]}
    assert uids(q=mixed_tags["c"][:12].lower()) == {mixed_tags["c"]}  # part of a UID, any case
    assert uids(q="reg-a") == {mixed_tags["a"]}
    assert uids(q="botany") == {mixed_tags["b"]}  # an item the tag was on EARLIER still leads to the tag
    assert uids(q="reg-b") == {mixed_tags["b"]} and uids(q="beta again") == {mixed_tags["b"]}
    assert uids(q="gamma") == {mixed_tags["c"]}
    assert uids(q="%") == set() and uids(q="no-such-thing") == set()  # wildcards are escaped
    assert client.get("/api/rfid/tags", headers=lib, params={"state": "bogus"}).status_code == 422
    page = client.get("/api/rfid/tags", headers=lib, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 3 and len(page["tags"]) == 1


@pytest.mark.req("FR05", "AC07", "NFR02")
def test_registry_is_readable_by_the_clerk_but_not_anonymous(client, lib, mixed_tags):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/rfid/tags", headers=clerk).status_code == 200
    client.cookies.clear()
    assert client.get("/api/rfid/tags").status_code == 401


# ================================================================== search finds items through old tags
@pytest.mark.req("FR02", "FR05")
def test_catalogue_search_traces_a_stray_tag_back_to_its_item(client, lib, mixed_tags):
    def search(uid):
        return client.get("/api/catalogue/items", headers=lib, params={"q": uid}).json()["items"]

    [active] = search(mixed_tags["a"])
    assert active["accession_no"] == "REG-A" and "matched_tag" not in active  # found normally
    [retired] = search(mixed_tags["c"].lower())
    assert retired["accession_no"] == "REG-C" and retired["matched_tag"] == {
        "uid": mixed_tags["c"],
        "status": "retired",
    }
    found = {i["accession_no"]: i for i in search(mixed_tags["b"])}  # the removed tag was on two items
    assert set(found) == {"REG-B", "REG-B2"} and found["REG-B"]["matched_tag"]["status"] == "removed"
    assert search(mixed_tags["a"][:10]) == []  # a partial UID is not a tag match


# ================================================================== migration 0004 keeps existing data
@pytest.mark.req("NFR06", "NFR01")
def test_migration_0004_preserves_tags_and_downgrade_works(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    migrate.upgrade(url, "0003")
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("INSERT INTO bib_records (dedupe_key, title, created_at) VALUES ('k1', 'T', '2026-01-01')")
    for n in (1, 2):
        con.execute(f"INSERT INTO items (bib_id, accession_no, created_at) VALUES (1, 'M{n}', '2026-01-01')")
    con.execute(
        "INSERT INTO rfid_tags (tag_uid, item_id, status, tagged_by, tagged_at) "
        "VALUES ('AAAA0001', 1, 'active', 'u', '2026-01-02')"
    )
    con.execute(
        "INSERT INTO rfid_tags (tag_uid, item_id, status, tagged_by, tagged_at, retired_at, retired_reason) "
        "VALUES ('BBBB0002', 2, 'retired', 'u', '2026-01-02', '2026-01-03', 'old reason')"
    )
    con.commit()
    con.close()

    migrate.upgrade(url)  # 0003 -> 0004
    con = sqlite3.connect(db)
    rows = con.execute("SELECT tag_uid, status, end_reason FROM rfid_tags ORDER BY id").fetchall()
    assert rows == [("AAAA0001", "active", ""), ("BBBB0002", "retired", "old reason")]  # nothing lost, nothing loosened
    con.execute(
        "INSERT INTO rfid_tags (tag_uid, item_id, status, tagged_by, tagged_at) "
        "VALUES ('AAAA0001', 2, 'removed', 'u', '2026-01-04')"
    )  # a UID may now appear in many links
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):  # but still only one ACTIVE link per tag and per item
        con.execute(
            "INSERT INTO rfid_tags (tag_uid, item_id, status, tagged_by, tagged_at) "
            "VALUES ('AAAA0001', 2, 'active', 'u', '2026-01-05')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO rfid_tags (tag_uid, item_id, status, tagged_by, tagged_at) "
            "VALUES ('CCCC0003', 1, 'active', 'u', '2026-01-05')"
        )
    con.close()

    migrate.downgrade(url, "0003")  # lossy by design: one row per tag, the latest link wins
    con = sqlite3.connect(db)
    back = dict(con.execute("SELECT tag_uid, status FROM rfid_tags").fetchall())
    assert back == {"AAAA0001": "retired", "BBBB0002": "retired"}  # the later 'removed' link of AAAA0001 wins
    con.close()
    migrate.upgrade(url)  # and it can go forward again
    assert count_rows(db) == 2


def count_rows(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM rfid_tags").fetchone()[0]
    finally:
        con.close()


@pytest.mark.req("NFR06")
def test_fresh_install_has_the_new_shape(client, settings):
    with make_engine(settings.database_url).connect() as c:
        c.execute(text("SELECT 1"))
    assert count(settings, "rfid_tags") == 0
    cols = {
        r[1]
        for r in sqlite3.connect(settings.database_url.removeprefix("sqlite:///")).execute(
            "PRAGMA table_info(rfid_tags)"
        )
    }
    assert {"ended_at", "end_reason"} <= cols and not ({"retired_at", "retired_reason"} & cols)


# ================================================================== pages
@pytest.mark.req("FR05", "FR11", "AC07")
def test_tag_registry_page_follows_permissions_and_navigation(client, lib):
    client.cookies.clear()
    assert client.get("/tags", follow_redirects=False).status_code in (302, 307)  # not signed in
    client.post("/api/auth/card-login", json={"uid": "STAFFAUDIT01"})  # auditor: no catalogue.read
    denied = client.get("/tags")
    assert denied.status_code == 403 and "cannot open this page" in denied.text
    assert 'href="/tags"' not in client.get("/dashboard").text

    client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"})  # clerk: may look, not change
    page = client.get("/tags")
    assert page.status_code == 200 and "Every RFID tag" in page.text and "const CAN_TAG = false" in page.text
    assert 'href="/tags"' in client.get("/dashboard").text

    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    assert "const CAN_TAG = true" in client.get("/tags").text


@pytest.mark.req("FR05", "AC02")
def test_tagging_page_offers_the_choice_of_what_happens_to_the_old_tag(client, lib):
    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    html = client.get("/tagging").text
    assert 'id="old-fate"' in html and "Retire it: damaged or lost" in html and "old_tag_fate" in html
    js = client.get("/static/ui.js").text
    assert "stateBadge" in js and "Retire tag" in js and "Remove tag" in js


# ===== reuse a removed tag (registry: "Reuse on another item") =====
@pytest.mark.req("FR05", "AC02")
def test_reuse_flow_as_the_pages_drive_it(client, lib, reader):
    """Registry -> tagging page with ?tag=UID: look the tag up, put THAT tag on the reader, link it to another item."""
    first, uid = tagged(client, lib, "RU1", "Wrongly Tagged")
    right = make_item(client, lib, "RU2", "Right Book")
    end(client, lib, uid, "remove", "wrong item")
    client.post(
        "/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"replace": True}
    )  # reader now holds other tags

    state = client.get(f"/api/rfid/tags/{uid}", headers=lib).json()["state"]
    assert state == "removed"  # the page checks this before offering reuse
    placed = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"uid": uid, "replace": True}).json()
    assert placed["uid"] == uid and placed["on_pad"] == 1  # exactly this tag, nothing else on the reader
    [seen] = client.post("/api/rfid/readers/staff-1/read", headers=lib).json()["tags"]
    assert seen["uid"] == uid and seen["usable"] is True and seen["state"] == "removed"

    r = tag(client, lib, right["id"], uid)
    assert r.status_code == 201 and r.json()["reused_tag"] is True
    assert reader.memory_of(uid).item_ref == "RU2"
    hist = client.get(f"/api/catalogue/items/{first['id']}", headers=lib).json()["tag_history"]
    assert [t["status"] for t in hist] == ["removed"]  # the first item's history is untouched


@pytest.mark.req("FR05")
def test_a_tag_that_cannot_be_reused_is_refused_before_it_is_offered(client, lib):
    _, active_uid = tagged(client, lib, "RU3")
    _, retired_uid = tagged(client, lib, "RU4")
    end(client, lib, retired_uid, "retire", "damaged")
    states = {u: client.get(f"/api/rfid/tags/{u}", headers=lib).json()["state"] for u in (active_uid, retired_uid)}
    assert states == {active_uid: "active", retired_uid: "retired"}  # the page shows "in use" / "retired", not reuse
    assert client.get("/api/rfid/tags/E280000000000009", headers=lib).status_code == 404  # unknown tag
    bad = client.post("/api/rfid/mock/readers/staff-1/tags", headers=lib, json={"uid": "not-hex!", "replace": True})
    assert bad.status_code == 422


@pytest.mark.req("FR05", "AC02")
def test_reuse_entry_points_exist_on_the_pages(client, lib):
    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    assert "/tagging?tag=" in client.get("/tags").text and "Reuse on another item" in client.get("/tags").text
    tg = client.get("/tagging").text
    assert "prepareReuse" in tg and 'get("tag")' in tg


@pytest.mark.req("FR05", "AC02")
def test_an_untagged_book_opened_for_tagging_starts_with_an_empty_simulator_reader(client, lib):
    page = client.get("/tagging", headers=lib).text
    assert "!it.tag && !reuse" in page  # the page takes the last book's tag off the reader on arrival
    assert "await putNextBook()" not in page.split("!it.tag && !reuse")[1][:300]  # and does not invent a new one
