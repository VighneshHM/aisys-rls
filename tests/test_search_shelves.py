"""FR01/FR02/FR06: full-text search, the public catalogue (OPAC), shelves and the virtual bookshelf, net cataloguing."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from conftest import login_card
from shelfsense import backup, migrate
from shelfsense.app import create_app
from shelfsense.catalogue import fts
from shelfsense.integrations.netcat import SAMPLE
from shelfsense.models import InventoryScan, InventorySession, Item
from shelfsense.shelves import service as shelves
from test_tagging import make_item

pytestmark = pytest.mark.req("FR01", "FR02", "FR06")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture
def db(client):
    with client.app.state.session_factory() as session:
        yield session


def add(client, lib, acc, title, **extra):
    return make_item(client, lib, acc, title, **extra)


def find(client, lib, q, **params):
    r = client.get("/api/catalogue/items", params={"q": q, **params}, headers=lib)
    assert r.status_code == 200, r.text
    return r.json()


def titles(res):
    return [i["title"] for i in res["items"]]


# ---------- full-text search ----------
def test_words_prefixes_and_every_word_must_match(client, lib):
    add(client, lib, "F1", "The Dispossessed", author="Ursula Le Guin", publisher="Harper")
    add(client, lib, "F2", "The Left Hand of Darkness", author="Ursula Le Guin", publisher="Ace")
    add(client, lib, "F3", "Darkness at Noon", author="Arthur Koestler")
    assert titles(find(client, lib, "dispos")) == ["The Dispossessed"], "an unfinished last word still finds it"
    assert sorted(titles(find(client, lib, "ursula guin"))) == ["The Dispossessed", "The Left Hand of Darkness"]
    assert titles(find(client, lib, "darkness koestler")) == ["Darkness at Noon"], "every word has to match"
    assert titles(find(client, lib, "harper")) == ["The Dispossessed"], "publisher is searchable"
    assert find(client, lib, "zebra")["total"] == 0


def test_better_matches_come_first(client, lib):
    add(client, lib, "R1", "Gardening for Beginners", author="A. Plant")
    add(client, lib, "R2", "Beginners guide", author="Ann Garden", publisher="Garden Press")
    add(client, lib, "R3", "Garden", author="X")
    res = titles(find(client, lib, "garden"))
    assert res[0] == "Garden", "a book whose title is the search word beats one that mentions it elsewhere"
    assert set(res) == {"Garden", "Gardening for Beginners", "Beginners guide"}


def test_the_old_contains_matching_still_works(client, lib):
    add(client, lib, "C1", "The Dispossessed", author="Ursula Le Guin")
    assert titles(find(client, lib, "ispossess")) == ["The Dispossessed"], "a fragment in the middle of a word"
    assert find(client, lib, "C1")["total"] == 1, "an accession number"


def test_accents_and_case_do_not_matter(client, lib):
    add(client, lib, "A1", "Café Society", author="José Álvarez")
    assert titles(find(client, lib, "CAFE")) == ["Café Society"]
    assert titles(find(client, lib, "jose alvarez")) == ["Café Society"]


@pytest.mark.parametrize(
    "q",
    [
        '"',
        '""',
        "'",
        "*",
        "OR",
        "AND",
        "NOT",
        "NEAR(a b)",
        "a OR b",
        "title:x",
        "-x",
        "(",
        ")",
        "^",
        "a*b",
        "{",
        "\\",
        "%",
        "_",
        ";--",
        "😀",
        "x" * 500,
        "a " * 40,
    ],
)
def test_search_syntax_typed_by_a_user_is_just_words(client, lib, q):
    add(client, lib, "S1", "Quiet Things")
    r = client.get("/api/catalogue/items", params={"q": q}, headers=lib)
    assert r.status_code == 200, (q, r.text)


def test_a_new_item_is_searchable_at_once_and_follows_edits_and_deletes(client, lib, db):
    item = add(client, lib, "RT1", "Lighthouse Keepers", author="Marit Solheim")
    assert titles(find(client, lib, "lighthouse")) == ["Lighthouse Keepers"], "searchable the moment it is saved"
    db.execute(text("UPDATE bib_records SET title = 'Harbour Pilots' WHERE id = :b"), {"b": item["bib_id"]})
    db.commit()
    assert titles(find(client, lib, "harbour")) == ["Harbour Pilots"]
    assert fts_rows(db, "lighthouse") == 0, "the old title left the index"
    db.execute(text("UPDATE items SET call_number = 'QX 99' WHERE id = :i"), {"i": item["id"]})
    db.commit()
    assert fts_rows(db, "qx") == 1
    db.execute(text("DELETE FROM items WHERE id = :i"), {"i": item["id"]})
    db.commit()
    assert fts_rows(db, "harbour") == 0


def fts_rows(db, word):
    return db.execute(text("SELECT count(*) FROM item_fts WHERE item_fts MATCH :m"), {"m": word}).scalar()


def test_loan_status_changes_do_not_touch_the_index(client, lib, db):
    item = add(client, lib, "ST1", "Steady Title")
    before = db.execute(text("SELECT count(*) FROM item_fts")).scalar()
    db.execute(text("UPDATE items SET status = 'on_loan' WHERE id = :i"), {"i": item["id"]})
    db.commit()
    assert db.execute(text("SELECT count(*) FROM item_fts")).scalar() == before


def test_search_still_works_without_the_index(client, lib, db):
    add(client, lib, "NF1", "The Dispossessed")
    db.execute(text("DROP TABLE item_fts"))  # also takes its triggers' target away: simulate a build without FTS5
    for name in ("trg_items_fts_ai", "trg_items_fts_au", "trg_items_fts_ad", "trg_bibs_fts_au"):
        db.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    db.commit()
    assert not fts.available(db)
    assert titles(find(client, lib, "dispossessed")) == ["The Dispossessed"]


def test_the_index_is_built_from_existing_items_by_the_migration(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    migrate.upgrade(url, "0009")
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute(
        "INSERT INTO bib_records(dedupe_key,title,author,created_at) "
        "VALUES ('k','Old Catalogue Entry','Some Author','2026-01-01')"
    )
    con.execute("INSERT INTO items(bib_id,accession_no,created_at) VALUES (1,'OLD1','2026-01-01')")
    con.commit()
    con.close()
    migrate.upgrade(url)
    con = sqlite3.connect(tmp_path / "old.db")
    assert con.execute("SELECT rowid FROM item_fts WHERE item_fts MATCH 'catalogue'").fetchall() == [(1,)]
    con.close()
    migrate.downgrade(url, "0009")
    con = sqlite3.connect(tmp_path / "old.db")
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE '%fts%'").fetchone()[0] == 0
    con.close()


def test_the_search_index_is_not_part_of_the_integrity_proof_and_survives_a_restore(client, lib, settings):
    add(client, lib, "BK1", "Backed Up Book")
    path = backup.sqlite_file(settings.database_url)
    assert not [t for t in backup.fingerprint(path) if t.startswith("item_fts")]
    info = backup.create_backup(settings.database_url, settings.backup_dir, "fts")
    add(client, lib, "BK2", "Added After Backup")
    backup.restore_backup(settings.database_url, info.path, settings.backup_dir)
    assert titles(find(client, lib, "backed")) == ["Backed Up Book"]
    assert find(client, lib, "after")["total"] == 0, "the index went back with the data"


# ---------- natural sort and the order check ----------
def test_call_numbers_sort_as_people_expect():
    order = sorted(["QA 10", "QA 9", "FIC LEG", "QA 9.2", "qa 2", ""], key=shelves.natural_key)
    assert order == ["", "FIC LEG", "qa 2", "QA 9", "QA 9.2", "QA 10"]


@pytest.mark.parametrize(
    "calls, expected_strays",
    [
        (["A 1", "A 2", "A 3"], []),
        ([], []),
        (["A 1"], []),
        (["A 1", "A 3", "A 2", "A 4"], ["A 3"]),  # one book out of place (either of the pair is a valid answer)
        (["A 4", "A 3", "A 2", "A 1"], ["A 4", "A 3", "A 2"]),  # a reversed shelf: keep one, move the rest
        (["A 1", "A 1", "A 1"], []),  # copies of the same book never count against each other
        (["B 2", "A 1", "A 2", "A 3"], ["B 2"]),  # one book that belongs at the end
    ],
)
def test_the_order_check_finds_the_fewest_books_to_move(calls, expected_strays):
    entries = [(i, cn) for i, cn in enumerate(calls)]
    moved = shelves.shelf_order_check(entries)
    if calls == ["A 1", "A 3", "A 2", "A 4"]:
        assert len(moved) == 1 and calls[moved[0]] in ("A 3", "A 2")
    else:
        assert [calls[i] for i in moved] == expected_strays
    kept = [cn for i, cn in entries if i not in moved]
    assert kept == sorted(kept, key=shelves.natural_key), "what stays is in order"


# ---------- shelves ----------
def put(db, client, lib, acc, title, call, location="Main/A1", **extra):
    return add(client, lib, acc, title, call_number=call, location=location, **extra)


def test_shelves_list_counts_and_managed_shelves(client, lib, db):
    put(db, client, lib, "L1", "One", "A 1")
    put(db, client, lib, "L2", "Two", "A 2")
    put(db, client, lib, "L3", "Three", "B 1", location="Annex")
    r = client.get("/api/shelves", headers=lib).json()["shelves"]
    by = {s["code"]: s for s in r}
    assert by["Main/A1"]["items"] == 2 and by["Annex"]["items"] == 1 and not by["Main/A1"]["managed"]
    created = client.post(
        "/api/shelves", json={"code": "Main/A1", "description": "Fiction A", "capacity": 1}, headers=lib
    )
    assert created.status_code == 201 and created.json()["over_capacity"] is True and created.json()["free"] == -1
    assert client.post("/api/shelves", json={"code": "Main/A1"}, headers=lib).status_code == 409
    sid = created.json()["id"]
    upd = client.patch(f"/api/shelves/{sid}", json={"capacity": 10}, headers=lib).json()
    assert upd["over_capacity"] is False and upd["free"] == 8
    assert client.post("/api/shelves", json={"code": "Empty/Z", "capacity": 5}, headers=lib).json()["items"] == 0
    assert client.delete(f"/api/shelves/{sid}", headers=lib).status_code == 204
    assert {s["code"] for s in client.get("/api/shelves", headers=lib).json()["shelves"]} >= {"Main/A1", "Annex"}
    assert client.delete(f"/api/shelves/{sid}", headers=lib).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"code": ""},
        {"code": "x" * 101},
        {"code": "A", "capacity": -1},
        {"code": "A", "capacity": "many"},
        {"code": "A", "capacity": 10**7},
    ],
)
def test_bad_shelf_input_is_refused(client, lib, body):
    assert client.post("/api/shelves", json=body, headers=lib).status_code == 422


def test_only_shelf_managers_change_shelves(client, lib):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/shelves", headers=clerk).status_code == 200
    assert client.post("/api/shelves", json={"code": "X"}, headers=clerk).status_code == 403
    client.cookies.clear()
    assert client.get("/api/shelves").status_code == 401


def test_the_bookshelf_is_in_call_number_order_and_hides_withdrawn_books(client, lib, db):
    put(db, client, lib, "V1", "Ten", "QA 10")
    put(db, client, lib, "V2", "Nine", "QA 9")
    put(db, client, lib, "V3", "Gone", "QA 1", status="withdrawn")
    put(db, client, lib, "V4", "Two", "QA 2")
    v = client.get("/api/shelves/view", params={"code": "Main/A1"}, headers=lib).json()
    assert [b["title"] for b in v["books"]] == ["Two", "Nine", "Ten"]
    assert v["last_sweep"] is None and all(not b["out_of_order"] for b in v["books"])
    assert client.get("/api/shelves/view", params={"code": ""}, headers=lib).status_code == 422
    assert client.get("/api/shelves/view", params={"code": "Nowhere"}, headers=lib).json()["books"] == []


def sweep(db, location, call_numbers_in_read_order, client, lib, prefix):
    """A closed inventory session whose handheld read these books in this order."""
    ids = []
    for n, cn in enumerate(call_numbers_in_read_order):
        ids.append(put(db, client, lib, f"{prefix}{n}", f"{prefix} book {cn}", cn, location=location)["id"])
    session = InventorySession(location=location, handheld_id="handheld-1", status="closed", started_by="lib", sweeps=1)
    db.add(session)
    db.flush()
    for n, item_id in enumerate(ids):
        db.add(
            InventoryScan(
                session_id=session.id, tag_uid=f"E200{prefix}{n:04d}", item_id=item_id, result="found", signal="ok"
            )
        )
    db.commit()
    return session


def test_a_sweep_shows_which_books_are_out_of_order(client, lib, db):
    s = sweep(db, "Main/A1", ["A 1", "A 3", "A 2", "A 4", "A 5"], client, lib, "O")
    v = client.get("/api/shelves/view", params={"code": "Main/A1"}, headers=lib).json()
    flagged = [b["call_number"] for b in v["books"] if b["out_of_order"]]
    assert len(flagged) == 1 and flagged[0] in ("A 2", "A 3")
    assert v["last_sweep"]["out_of_order"] == 1 and v["last_sweep"]["session_id"] == s.id


def test_the_inventory_report_includes_the_order_check(client, lib, db):
    s = sweep(db, "Main/A1", ["B 2", "A 1", "A 2"], client, lib, "I")
    rep = client.get(f"/api/inventory/sessions/{s.id}", headers=lib).json()
    assert rep["shelf_order"]["in_order"] is False and rep["shelf_order"]["out_of_order"][0]["call_number"] == "B 2"
    s2 = sweep(db, "Annex", ["A 1", "A 2", "A 3"], client, lib, "J")
    assert client.get(f"/api/inventory/sessions/{s2.id}", headers=lib).json()["shelf_order"] == {
        "checked": 3,
        "out_of_order": [],
        "in_order": True,
    }


# ---------- the public catalogue ----------
def public(client):
    client.cookies.clear()
    return client


def test_the_opac_needs_no_sign_in_and_shows_only_what_a_visitor_needs(client, lib):
    add(
        client,
        lib,
        "P1",
        "Public Poems",
        author="Ann Writer",
        publisher="Open Press",
        barcode="BC0001",
        call_number="821 WRI",
        location="Main/P",
    )
    add(
        client,
        lib,
        "P2",
        "Public Poems",
        author="Ann Writer",
        publisher="Open Press",
        call_number="821 WRI",
        location="Main/P",
        status="damaged",
    )
    add(client, lib, "P3", "Public Hidden", status="withdrawn")
    r = public(client).get("/api/opac/search", params={"q": "poems"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["results"][0]["available"] == 1
    assert [c["availability"] for c in body["results"][0]["copies"]] == ["Available", "Unavailable (being repaired)"]
    text_ = r.text
    for secret in ("P1", "P2", "BC0001", "accession", "barcode", "tag", "member", "loan", "due"):
        assert secret not in text_, f"{secret} must not be shown to a visitor"
    assert public(client).get("/api/opac/search", params={"q": "hidden"}).json()["total"] == 0, (
        "withdrawn books are not listed"
    )


def test_the_opac_labels_reference_and_loaned_books(client, lib, db):
    add(client, lib, "Q1", "Atlas of Things", item_type="reference", location="Ref")
    loaned = add(client, lib, "Q2", "Atlas of Things", location="Main/P")
    db.execute(text("UPDATE items SET status = 'on_loan' WHERE id = :i"), {"i": loaned["id"]})
    db.commit()
    copies = public(client).get("/api/opac/search", params={"q": "atlas"}).json()["results"][0]["copies"]
    assert {c["availability"] for c in copies} == {"Reference: use in the library", "On loan"}


@pytest.mark.parametrize("q", ["", "a", " ", "x" * 101])
def test_the_opac_wants_a_real_search(client, q):
    assert public(client).get("/api/opac/search", params={"q": q}).status_code == 422


def test_the_opac_count_and_paging_cover_every_match(client, lib, monkeypatch):
    """D-074: the search read only the first 200 copies, so total stopped at 200 and later pages were empty."""
    from shelfsense import opac_routes

    opac_routes._hits.clear()
    for i in range(230):
        add(client, lib, f"CW{i:03d}", f"Common Word {i:03d}")
    for i in range(30):  # hidden copies must not use up the room the visible ones need
        add(client, lib, f"CH{i:03d}", f"Common Word hidden {i:03d}", status="withdrawn")
    body = public(client).get("/api/opac/search", params={"q": "common word", "limit": 50, "offset": 200}).json()
    assert body["total"] == 230 and body["complete"] is True and len(body["results"]) == 30
    monkeypatch.setattr(opac_routes, "MAX_COPIES", 100)
    capped = public(client).get("/api/opac/search", params={"q": "common word"}).json()
    assert capped["complete"] is False and capped["total"] == 100  # says it is not the whole answer
    long_q = public(client).get("/api/opac/search", params={"q": "x" * 101})
    assert long_q.status_code == 422 and "at most 100" in long_q.text  # D-075, not "at least 2"
    opac_routes._hits.clear()


def test_the_opac_limits_how_fast_one_visitor_can_search(client, lib, monkeypatch):
    from shelfsense import opac_routes

    opac_routes._hits.clear()
    monkeypatch.setattr(opac_routes, "LIMIT_PER_MINUTE", 5)
    codes = [public(client).get("/api/opac/search", params={"q": "quiet"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    opac_routes._hits.clear()


def test_the_opac_shelf_view_shows_titles_only(client, lib):
    add(client, lib, "W1", "Shelf Book", call_number="X 1", location="Main/W", barcode="BCW1")
    r = public(client).get("/api/opac/shelf", params={"code": "Main/W"})
    assert r.status_code == 200 and r.json()["books"][0]["title"] == "Shelf Book"
    assert "W1" not in r.text and "BCW1" not in r.text


def test_the_opac_can_be_switched_off(settings):
    off = dataclasses.replace(settings, opac_enabled=False)
    with TestClient(create_app(off)) as c:
        assert c.get("/api/opac/search", params={"q": "anything"}).status_code == 404
        assert c.get("/opac").status_code == 404


def test_the_opac_page_is_public(client):
    r = public(client).get("/opac")
    assert r.status_code == 200 and "Find a book" in r.text


# ---------- net cataloguing ----------
def test_isbn_lookup_fills_the_form_and_the_result_can_be_saved(client, lib):
    isbn, rec = next(iter(SAMPLE.items()))
    r = client.get("/api/catalogue/lookup", params={"isbn": isbn}, headers=lib)
    assert r.status_code == 200 and r.json()["title"] == rec["title"] and r.json()["source"] == "mock"
    dashed = f"{isbn[:3]}-{isbn[3:4]}-{isbn[4:7]}-{isbn[7:12]}-{isbn[12]}"
    assert client.get("/api/catalogue/lookup", params={"isbn": dashed}, headers=lib).status_code == 200
    saved = add(
        client,
        lib,
        "NC1",
        r.json()["title"],
        **{k: r.json()[k] for k in ("author", "publisher", "pub_year", "isbn", "language", "edition")},
    )
    assert saved["isbn"] == isbn and saved["author"] == rec["author"]


@pytest.mark.parametrize("isbn", ["", "123", "9780001000010", "abcdefghijklm", "978-0-00-100000-9 x", "9" * 20])
def test_a_bad_isbn_is_refused_before_asking_anyone(client, lib, isbn):
    assert client.get("/api/catalogue/lookup", params={"isbn": isbn}, headers=lib).status_code == 422


def test_an_unknown_book_and_an_outside_outage_have_clear_answers(client, lib):
    unknown = "9781234567897"
    assert client.get("/api/catalogue/lookup", params={"isbn": unknown}, headers=lib).status_code == 404
    isbn = next(iter(SAMPLE))
    client.app.state.netcat.fail_next(1)
    r = client.get("/api/catalogue/lookup", params={"isbn": isbn}, headers=lib)
    assert r.status_code == 502 and "by hand" in r.json()["detail"]["message"]
    assert client.get("/api/catalogue/lookup", params={"isbn": isbn}, headers=lib).status_code == 200, "and it recovers"


def test_isbn_lookup_needs_catalogue_write(client):
    clerk = login_card(client, "STAFFCLERK01")
    assert client.get("/api/catalogue/lookup", params={"isbn": next(iter(SAMPLE))}, headers=clerk).status_code == 403


def test_the_sample_records_are_valid_and_synthetic():
    from shelfsense.migration import validation

    assert len(SAMPLE) >= 10 and all(validation.isbn_valid(i) for i in SAMPLE)
    assert Item  # imported for the type checkers of the readers of this file
    assert select  # (kept: the module is also a quick index of what these tests touch)


def test_a_bad_netcat_driver_stops_the_app_at_start(settings):
    bad = dataclasses.replace(settings, netcat_driver="nonesuch")
    with pytest.raises(ValueError, match="Unknown net-cataloguing driver"):
        create_app(bad)


def test_pages_render(client, lib):
    client.cookies.set("shelfsense_session", lib["Authorization"].split()[1])
    assert client.get("/shelves").status_code == 200
    clerk = login_card(client, "STAFFCLERK01")
    client.cookies.set("shelfsense_session", clerk["Authorization"].split()[1])
    assert client.get("/shelves").status_code == 200
