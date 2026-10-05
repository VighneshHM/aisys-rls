"""AC01 / FR10 / NFR01: spreadsheet migration pipeline."""

from __future__ import annotations

import io
import sqlite3
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from aisys.db import make_engine
from aisys.devtools import sample_data
from aisys.migration import mapping, validation
from aisys.migration import service as mig_service
from conftest import login_card


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")  # librarian: has migration.run


def csv_bytes(headers, rows) -> bytes:
    out = io.StringIO()
    import csv

    w = csv.writer(out)
    w.writerow(headers)
    w.writerows(rows)
    return out.getvalue().encode("utf-8")


def sample_csv(n=400, seed=42):
    headers, rows, manifest = sample_data.build_rows(n, seed)
    return csv_bytes(headers, rows), headers, rows, manifest


def upload(client, headers_auth, data: bytes, name="holdings.csv", **form):
    return client.post("/api/migrations", headers=headers_auth, files={"file": (name, data, "text/csv")}, data=form)


def count(settings, table, where="1=1") -> int:
    with make_engine(settings.database_url).connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()


# ------------------------------------------------------------------ unit: rules
@pytest.mark.req("AC01", "FR10")
def test_isbn_check_digits():
    assert validation.isbn_valid("9780306406157") and validation.isbn_valid("0306406152")
    assert validation.isbn_valid("080442957X")
    assert not validation.isbn_valid("9780306406158") and not validation.isbn_valid("123")
    assert validation.normalize_isbn("ISBN-13: 978-0-306-40615-7") == "9780306406157"


@pytest.mark.req("AC01", "FR10")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,200.50", "1200.50"), ("₹ 450", "450.00"), ("12,50", "12.50"), ("$12", "12.00"), ("-5", "-5.00")],
)
def test_price_parsing(raw, expected):
    assert str(validation.parse_price(raw)) == expected


@pytest.mark.req("AC01", "FR10")
def test_row_rules_errors_vs_warnings():
    ok = validation.validate_row({"title": "A Title", "accession_no": "ACC1", "item_type": "Reference"})
    assert ok.ok and ok.clean["is_reference"] is True
    bad = validation.validate_row({"title": "", "accession_no": "bad id!", "isbn": "123", "price": "x"})
    assert {i.code for i in bad.issues if i.severity == "error"} >= {
        "required_missing", "accession_format", "isbn_invalid", "price_invalid",
    }  # fmt: skip
    warn = validation.validate_row(
        {"title": "T", "accession_no": "A", "item_type": "Novel", "status": "Issued", "pub_year": "x"}
    )
    assert warn.ok and {i.code for i in warn.issues} == {"item_type_unknown", "loan_not_migrated", "year_invalid"}


@pytest.mark.req("FR10")
def test_mapping_suggestion_covers_sample_headers():
    suggested, unmapped = mapping.suggest_mapping(sample_data.HEADERS)
    assert set(suggested) == set(mapping.FIELDS) and not unmapped
    assert suggested["accession_no"] == "Acc. No." and suggested["pub_year"] == "Year"
    fuzzy, _ = mapping.suggest_mapping(["Accesion Number", "Titel", "ISBN-13"])  # typos still map
    assert fuzzy.get("accession_no") == "Accesion Number" and fuzzy.get("isbn") == "ISBN-13"


# ------------------------------------------------------------------ AC01: staging + validation
@pytest.mark.req("AC01", "FR10")
def test_import_stages_and_validates_without_touching_library(client, lib, settings):
    data, _, _, manifest = sample_csv()
    r = upload(client, lib, data)
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "validated"
    s = b["summary"]
    assert (s["valid"], s["invalid"], s["duplicate"]) == (
        manifest["expected_valid"], manifest["expected_invalid"], manifest["expected_duplicate"],
    )  # fmt: skip
    assert s["valid_with_warnings"] == manifest["expected_valid_with_warnings"]
    assert s["valid"] + s["invalid"] + s["duplicate"] == s["source_rows"] == manifest["rows"]
    assert count(settings, "migration_rows") == manifest["rows"]  # everything staged
    assert count(settings, "items") == 0 and count(settings, "bib_records") == 0  # dry run wrote nothing


@pytest.mark.req("AC01", "FR10")
def test_invalid_rows_and_duplicates_are_identified_per_row(client, lib):
    data, headers, rows, _ = sample_csv()
    bid = upload(client, lib, data).json()["id"]
    inv = client.get(f"/api/migrations/{bid}/rows?status=invalid&limit=500", headers=lib).json()
    assert inv["total"] > 0
    assert all(any(e["severity"] == "error" for e in r["errors"]) for r in inv["rows"])
    dup = client.get(f"/api/migrations/{bid}/rows?status=duplicate&limit=500", headers=lib).json()
    assert dup["total"] > 0 and all(r["duplicate_of"].startswith("row ") for r in dup["rows"])


# ------------------------------------------------------------------ AC01: commit + reconcile
@pytest.mark.req("AC01", "FR10", "NFR01")
def test_commit_migrates_valid_rows_and_reconciles(client, lib, settings):
    data, _, _, manifest = sample_csv()
    bid = upload(client, lib, data).json()["id"]
    r = client.post(f"/api/migrations/{bid}/commit", headers=lib)
    assert r.status_code == 200, r.text
    rec = r.json()["summary"]["reconciliation"]
    assert rec["migrated"] == manifest["expected_valid"] == rec["items_added"] == count(settings, "items")
    assert rec["source_rows"] == rec["migrated"] + rec["rejected_invalid"] + rec["skipped_duplicate"]
    assert rec["balanced"] is True and rec["existing_records_unchanged"] is True
    assert count(settings, "bib_records") == rec["bibs_created"] > 0
    assert count(settings, "migration_rows", "status='migrated'") == manifest["expected_valid"]
    assert r.json()["status"] == "committed"


@pytest.mark.req("NFR01", "AC10")
def test_backup_taken_before_commit_holds_pre_migration_state(client, lib, settings):
    data, _, _, _ = sample_csv()
    bid = upload(client, lib, data).json()["id"]
    path = client.post(f"/api/migrations/{bid}/commit", headers=lib).json()["backup"]
    assert Path(path).is_file() and "pre-migration" in path
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0  # snapshot predates the import
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert count(settings, "items") > 0


@pytest.mark.req("NFR01", "AC01")
def test_existing_records_are_never_modified_and_overlap_is_skipped(client, lib, settings):
    data, _, _, _ = sample_csv()
    first = upload(client, lib, data).json()["id"]
    client.post(f"/api/migrations/{first}/commit", headers=lib)
    with make_engine(settings.database_url).connect() as c:
        before = c.execute(text("SELECT * FROM items ORDER BY id")).fetchall()
    # re-import the same file: every previously migrated row is now a duplicate of an existing item
    again = upload(client, lib, data).json()
    assert again["summary"]["valid"] == 0 and again["summary"]["duplicate"] >= first_valid(client, lib, first)
    assert client.post(f"/api/migrations/{again['id']}/commit", headers=lib).status_code == 422
    with make_engine(settings.database_url).connect() as c:
        assert c.execute(text("SELECT * FROM items ORDER BY id")).fetchall() == before


def first_valid(client, lib, bid) -> int:
    return client.get(f"/api/migrations/{bid}", headers=lib).json()["summary"]["valid"]


@pytest.mark.req("NFR01", "FR10")
def test_second_batch_adds_copies_to_existing_bib_records(client, lib, settings):
    headers = ["Acc No", "Title", "Author", "ISBN"]
    a = csv_bytes(headers, [["A1", "Shared Book", "Ann", "9780306406157"]])
    b = csv_bytes(headers, [["A2", "Shared Book", "Ann", "9780306406157"], ["A1", "dup", "x", ""]])
    id_a = upload(client, lib, a).json()["id"]
    client.post(f"/api/migrations/{id_a}/commit", headers=lib)
    sb = upload(client, lib, b).json()
    assert sb["summary"]["bibs_reused"] == 1 and sb["summary"]["bibs_to_create"] == 0
    assert sb["summary"]["duplicate"] == 1
    client.post(f"/api/migrations/{sb['id']}/commit", headers=lib)
    assert count(settings, "bib_records") == 1 and count(settings, "items") == 2  # one bib, two copies


# ------------------------------------------------------------------ rollback & atomicity
@pytest.mark.req("AC01", "FR10", "AC10")
def test_rollback_restores_pre_migration_state(client, lib, settings):
    small = csv_bytes(["Acc No", "Title"], [["P1", "Pre-existing"]])
    pid = upload(client, lib, small).json()["id"]
    client.post(f"/api/migrations/{pid}/commit", headers=lib)

    data, _, _, _ = sample_csv()
    bid = upload(client, lib, data).json()["id"]
    client.post(f"/api/migrations/{bid}/commit", headers=lib)
    assert count(settings, "items") > 1
    r = client.post(f"/api/migrations/{bid}/rollback", headers=lib)
    assert r.status_code == 200, r.text
    rb = r.json()["summary"]["rollback"]
    assert rb["restored_to_pre_migration_counts"] is True and rb["items_now"] == 1
    assert count(settings, "items") == 1 and count(settings, "bib_records") == 1  # earlier batch untouched
    assert r.json()["status"] == "rolled_back"
    assert client.post(f"/api/migrations/{bid}/rollback", headers=lib).status_code == 409
    assert client.post(f"/api/migrations/{bid}/commit", headers=lib).status_code == 409


@pytest.mark.req("AC01", "AC10", "NFR01")
def test_failed_commit_is_atomic_and_leaves_library_unchanged(client, lib, settings, monkeypatch):
    base = csv_bytes(["Acc No", "Title"], [["E1", "Existing"]])
    eid = upload(client, lib, base).json()["id"]
    client.post(f"/api/migrations/{eid}/commit", headers=lib)

    data, _, _, _ = sample_csv()
    bid = upload(client, lib, data).json()["id"]

    def boom(*a, **k):
        raise RuntimeError("simulated failure after inserts")

    monkeypatch.setattr(mig_service, "_reconcile", boom)  # fails AFTER rows were inserted in the transaction
    r = client.post(f"/api/migrations/{bid}/commit", headers=lib)
    assert r.status_code == 500 and "unchanged" in r.json()["detail"]
    assert count(settings, "items") == 1 and count(settings, "bib_records") == 1
    got = client.get(f"/api/migrations/{bid}", headers=lib).json()
    assert got["status"] == "failed" and Path(got["summary"]["backup_path"]).is_file()


# ------------------------------------------------------------------ input handling
@pytest.mark.req("FR10")
def test_missing_required_column_needs_mapping_then_recovers(client, lib):
    data = csv_bytes(["Code", "Name of work"], [["X1", "Alpha"], ["X2", "Beta"]])
    b = upload(client, lib, data).json()
    assert b["status"] == "staged" and b["summary"]["mapping_problems"]
    assert client.post(f"/api/migrations/{b['id']}/commit", headers=lib).status_code == 409
    fixed = client.put(f"/api/migrations/{b['id']}/mapping", headers=lib,
                       json={"mapping": {"accession_no": "Code", "title": "Name of work"}})  # fmt: skip
    assert fixed.status_code == 200 and fixed.json()["status"] == "validated" and fixed.json()["summary"]["valid"] == 2
    bad = client.put(f"/api/migrations/{b['id']}/mapping", headers=lib, json={"mapping": {"title": "Nope"}})
    assert bad.json()["status"] == "staged"


@pytest.mark.req("FR10", "NFR02")
def test_rejects_bad_files(client, lib):
    assert upload(client, lib, b"x", "notes.txt").status_code == 422
    assert upload(client, lib, b"", "empty.csv").status_code == 422
    assert upload(client, lib, b"not a zip", "broken.xlsx").status_code == 422
    assert client.post("/api/migrations", headers=lib, files={"file": ("a.csv", b"a,b\n1,2", "text/csv")},
                       data={"mapping": "[1,2]"}).status_code == 422  # fmt: skip


@pytest.mark.req("FR10")
def test_semicolon_cp1252_and_bom_files(client, lib):
    body = "Accession;Title;Author\nZ1;Café Culture;Zoë\nZ2;Plain;Bob\n"
    for enc in ("cp1252", "utf-8-sig"):
        b = upload(client, lib, body.encode(enc)).json()
        assert b["summary"]["valid"] == 2, (enc, b["summary"])
    rows = client.get(f"/api/migrations/{b['id']}/rows", headers=lib).json()["rows"]
    assert rows[0]["raw"]["Title"] == "Café Culture"


@pytest.mark.req("FR10", "NFR02")
def test_error_report_csv_neutralises_formula_injection(client, lib):
    data = csv_bytes(["Acc No", "Title", "Author"], [["", '=HYPERLINK("http://evil")', "+cmd"], ["OK1", "Fine", ""]])
    bid = upload(client, lib, data).json()["id"]
    r = client.get(f"/api/migrations/{bid}/errors.csv", headers=lib)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    lines = r.text.splitlines()
    assert lines[0].startswith("row_no,row_status,severity,field,code,message")
    assert "'=HYPERLINK" in r.text and "'+cmd" in r.text and ",=HYPERLINK" not in r.text
    assert any("required_missing" in line for line in lines)


@pytest.mark.req("FR10", "NFR02")
def test_migration_is_permission_protected_and_audited(client, lib, clerk):
    data = csv_bytes(["Acc No", "Title"], [["A", "T"]])
    client.cookies.clear()  # the test client keeps the session cookie from the fixtures
    assert client.post("/api/migrations", files={"file": ("a.csv", data)}).status_code == 401
    assert upload(client, clerk, data).status_code == 403  # clerk lacks migration.run
    bid = upload(client, lib, data).json()["id"]
    client.post(f"/api/migrations/{bid}/commit", headers=lib)
    client.post(f"/api/migrations/{bid}/rollback", headers=lib)
    aud = login_card(client, "STAFFAUDIT01")
    actions = [e["action"] for e in client.get("/api/audit?action=migration&limit=50", headers=aud).json()]
    assert {"migration.upload", "migration.validate", "migration.commit", "migration.rollback"} <= set(actions)


# ------------------------------------------------------------------ AC01 at full scale
@pytest.mark.req("AC01", "FR10", "NFR05")
def test_full_20000_row_xlsx_import_reconcile_and_rollback(client, lib, settings, tmp_path):
    headers, rows, manifest = sample_data.build_rows(20000, 42)
    xlsx = tmp_path / "library_20000.xlsx"
    sample_data.write_xlsx(xlsx, headers, rows)

    t0 = time.perf_counter()
    r = client.post("/api/migrations", headers=lib, files={"file": (xlsx.name, xlsx.read_bytes())})
    t_validate = time.perf_counter() - t0
    assert r.status_code == 201, r.text
    s = r.json()["summary"]
    assert (s["valid"], s["invalid"], s["duplicate"]) == (
        manifest["expected_valid"], manifest["expected_invalid"], manifest["expected_duplicate"],
    )  # fmt: skip
    bid = r.json()["id"]

    t0 = time.perf_counter()
    c = client.post(f"/api/migrations/{bid}/commit", headers=lib)
    t_commit = time.perf_counter() - t0
    rec = c.json()["summary"]["reconciliation"]
    assert rec["balanced"] and rec["migrated"] == manifest["expected_valid"] == count(settings, "items")

    rb = client.post(f"/api/migrations/{bid}/rollback", headers=lib).json()["summary"]["rollback"]
    assert rb["items_now"] == 0 and rb["restored_to_pre_migration_counts"]
    print(f"\n20k rows: upload+validate {t_validate:.1f}s, commit {t_commit:.1f}s")  # recorded as performance evidence
    assert t_validate < 90 and t_commit < 90  # generous ceiling; real numbers are printed above


@pytest.mark.req("AC01", "FR10")
def test_reported_row_numbers_match_the_spreadsheet(client, lib):
    """Row numbers in reports must match what a person sees in Excel (header = row 1, blank lines counted)."""
    data = b"Acc No,Title\n\nA1,First\nA2,\nA1,Dup of first\n"  # line 2 blank, line 4 invalid, line 5 duplicate
    bid = upload(client, lib, data).json()["id"]
    bad = client.get(f"/api/migrations/{bid}/rows?status=invalid", headers=lib).json()["rows"]
    dup = client.get(f"/api/migrations/{bid}/rows?status=duplicate", headers=lib).json()["rows"]
    assert [r["row_no"] for r in bad] == [4]
    assert dup[0]["row_no"] == 5 and dup[0]["duplicate_of"] == "row 3 in this file"


@pytest.mark.req("AC01", "FR10", "FR11")
def test_migration_page_is_permission_gated(client, lib, clerk):
    client.cookies.clear()
    assert client.get("/migration", follow_redirects=False).status_code in (302, 307)
    client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"})
    denied = client.get("/migration")
    assert denied.status_code == 403 and "cannot open this page" in denied.text
    client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"})
    ok = client.get("/migration")
    assert ok.status_code == 200 and "Import holdings" in ok.text and "innerHTML" not in ok.text


@pytest.mark.req("AC01", "FR10")
def test_warnings_on_skipped_rows_are_not_reported_as_imported(client, lib):
    data = csv_bytes(
        ["Acc No", "Title", "Type", "Price"], [["W1", "Ok", "Novel", "5"], ["W2", "Skipped", "Novel", "N/A"]]
    )
    s = upload(client, lib, data).json()["summary"]
    assert s["valid"] == 1 and s["invalid"] == 1
    assert s["warning_codes"] == {"item_type_unknown": 1} and s["valid_with_warnings"] == 1
