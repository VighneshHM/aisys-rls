"""AC10: restore from a backup after a failed migration or a damaging change, and prove the original data is intact."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aisys import backup, migrate
from aisys.app import create_app
from aisys.backup import BackupError
from aisys.cli import main as cli_main
from aisys.cli import seed_demo
from conftest import login_card
from test_circulation import book, out, sql
from test_migration import sample_csv, upload
from test_tagging import make_item, place, tag


@pytest.fixture
def admin_h(client):
    return login_card(client, "STAFFADMIN01")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def db_file(settings) -> Path:
    return Path(settings.database_url.removeprefix("sqlite:///"))


def make_library(client, lib):
    """Some real data: tagged books, a loan, and the demo members (one owing money, one blocked)."""
    for n in range(1, 11):
        item = make_item(client, lib, f"B{n}", f"Backup Book {n}", location=f"Shelf-{n % 3}")
        assert tag(client, lib, item["id"], place(client, lib)).status_code == 201
    assert out(client, lib, "M0001", "B1").status_code == 200


def create(client, hdr, label="manual", **kw):
    r = client.post("/api/backups", headers=hdr, json={"label": label, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def compare(client, hdr, name):
    r = client.get(f"/api/backups/{name}/compare", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def restore(client, hdr, name, confirm="RESTORE"):
    return client.post(f"/api/backups/{name}/restore", headers=hdr, json={"confirm": confirm})


# ================================================================== the proof tool
@pytest.mark.req("AC10", "NFR01")
def test_the_fingerprint_notices_one_changed_cell_and_nothing_else(client, lib, settings):
    make_library(client, lib)
    path = db_file(settings)
    before = backup.fingerprint(path)
    assert backup.fingerprint(path) == before  # reading changes nothing, and the result is stable
    sql(settings, "UPDATE bib_records SET title = 'Backup Book 1!' WHERE title = 'Backup Book 1'")
    after = backup.fingerprint(path)
    assert [t for t in before if before[t] != after[t]] == ["bib_records"]  # exactly one table, and the same row count
    assert before["bib_records"]["rows"] == after["bib_records"]["rows"]
    assert before["items"] == after["items"] and before["members"] == after["members"]


@pytest.mark.req("AC10")
def test_compare_reports_table_by_table_and_treats_activity_separately(client, lib, admin_h, settings):
    make_library(client, lib)
    name = create(client, admin_h)["name"]
    same = compare(client, admin_h, name)
    assert same["data_intact"] is True and same["differing"] == []
    login_card(client, "STAFFCLERK01")  # signing in adds a session and audit rows: ordinary activity
    after_use = compare(client, admin_h, name)
    assert after_use["data_intact"] is True and "sessions" in after_use["activity_since"]  # not counted as damage
    sql(settings, "UPDATE members SET fines_cents = 0")
    sql(settings, "CREATE TABLE stray (x)")
    broken = compare(client, admin_h, name)
    assert broken["data_intact"] is False and "members" in broken["differing"] and "stray" in broken["differing"]
    status = {t["table"]: t["status"] for t in broken["tables"]}
    assert status["members"] == "changed" and status["stray"] == "only_in_live" and status["items"] == "same"


# ================================================================== making and checking backups
@pytest.mark.req("AC10", "NFR01")
def test_a_backup_is_one_verified_file_with_a_manifest(client, lib, admin_h, settings):
    make_library(client, lib)
    b = create(client, admin_h, "before-update", note="nightly")
    folder = Path(settings.backup_dir)
    assert (
        b["label"] == "before-update"
        and b["note"] == "nightly"
        and b["revision"] == backup.head()
        and b["has_manifest"]
    )
    assert sorted(p.name for p in folder.iterdir()) == [b["name"], b["name"] + ".json"]  # no -wal / -shm beside it
    v = client.get(f"/api/backups/{b['name']}/verify", headers=admin_h).json()
    assert v["ok"] and v["integrity_ok"] and v["checksum_matches"] is True and v["needs_upgrade"] is False
    listed = client.get("/api/backups", headers=admin_h).json()
    assert [x["name"] for x in listed["backups"]] == [b["name"]] and listed["head"] == backup.head()
    assert "backup.create" in {a for (a,) in sql(settings, "SELECT action FROM audit_events")}


@pytest.mark.req("AC10", "NFR01")
def test_verify_catches_tampering_damage_and_foreign_files(client, lib, admin_h, settings):
    make_library(client, lib)
    folder = Path(settings.backup_dir)
    good = create(client, admin_h)["name"]
    tampered = create(client, admin_h, "tampered")["name"]
    with (folder / tampered).open("ab") as fh:
        fh.write(b"\0" * 16)  # altered after it was made
    v = client.get(f"/api/backups/{tampered}/verify", headers=admin_h).json()
    assert (
        v["ok"] is False and v["checksum_matches"] is False and "changed since it was made" in " ".join(v["problems"])
    )
    (folder / "garbage.db").write_bytes(b"this is not a database at all" * 50)
    assert client.get("/api/backups/garbage.db/verify", headers=admin_h).json()["integrity_ok"] is False
    other = folder / "foreign.db"  # a healthy SQLite file, but not ours
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    f = client.get("/api/backups/foreign.db/verify", headers=admin_h).json()
    assert f["ok"] is False and "not a backup of this application" in " ".join(f["problems"])
    newer = folder / "newer.db"  # made by a newer version: a schema revision this app has never heard of
    (folder / good).replace(newer)
    con = sqlite3.connect(newer)
    con.execute("UPDATE alembic_version SET version_num = '9999'")
    con.commit()
    con.close()
    n = client.get("/api/backups/newer.db/verify", headers=admin_h).json()
    assert n["ok"] is False and "newer version" in " ".join(n["problems"])
    hand_copy = folder / "handmade.db"  # a copy made by hand has no manifest, and is still usable
    sqlite3.connect(db_file(settings)).backup(sqlite3.connect(hand_copy))
    h = client.get("/api/backups/handmade.db/verify", headers=admin_h).json()
    assert h["ok"] and h["manifest_present"] is False and h["checksum_matches"] is None


# ================================================================== restore: the heart of AC10
@pytest.mark.req("AC10", "NFR01")
def test_restore_after_a_damaging_change_proves_the_original_data_is_back(client, lib, admin_h, settings):
    make_library(client, lib)
    original = backup.fingerprint(db_file(settings))
    name = create(client, admin_h, "before-change")["name"]
    items_before = client.get("/api/catalogue/items?limit=50", headers=lib).json()["total"]

    damage = client.post("/api/backups/mock/damage", headers=admin_h).json()  # the faulty change
    assert damage["rows_touched"] > 0
    broken = compare(client, admin_h, name)
    assert broken["data_intact"] is False
    assert {"members", "items", "rfid_tags", "loans"} <= set(broken["differing"])  # it shows exactly what was hurt
    assert backup.fingerprint(db_file(settings)) != original

    assert restore(client, admin_h, name, confirm="yes please").status_code == 422  # the word must be RESTORE
    assert backup.fingerprint(db_file(settings)) != original  # and nothing happened
    r = restore(client, admin_h, name)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["proof"]["data_intact"] is True and all(t["match"] for t in result["proof"]["tables"])
    assert result["safety_backup"].startswith("pre-restore-") and result["upgraded_to"] is None

    now = backup.fingerprint(db_file(settings))
    for table, fp in original.items():  # the proof, checked independently of the app's own comparison
        if table not in backup.VOLATILE_TABLES:
            assert now[table] == fp, table
    assert compare(client, admin_h, name)["data_intact"] is True
    assert (
        client.get("/api/catalogue/items?limit=50", headers=lib).json()["total"] == items_before
    )  # the app still works
    assert "backup.restore" in {a for (a,) in sql(settings, "SELECT action FROM audit_events")}


@pytest.mark.req("AC10", "NFR01")
def test_a_restore_can_itself_be_undone_with_its_safety_copy(client, lib, admin_h, settings):
    make_library(client, lib)
    name = create(client, admin_h)["name"]
    client.post("/api/backups/mock/damage", headers=admin_h)
    damaged = backup.fingerprint(db_file(settings))
    safety = restore(client, admin_h, name).json()["safety_backup"]
    assert backup.fingerprint(db_file(settings)) != damaged
    again = restore(client, admin_h, safety)  # restore the safety copy: back to the state just before the restore
    assert again.status_code == 200
    back = backup.fingerprint(db_file(settings))
    for table, fp in damaged.items():
        if table not in backup.VOLATILE_TABLES:
            assert back[table] == fp, table


@pytest.mark.req("AC10", "NFR01")
def test_a_restore_that_does_not_check_out_puts_the_previous_data_back(client, lib, admin_h, settings, monkeypatch):
    make_library(client, lib)
    name = create(client, admin_h)["name"]
    client.post("/api/backups/mock/damage", headers=admin_h)
    damaged = backup.fingerprint(db_file(settings))
    real = backup.compare
    monkeypatch.setattr(backup, "compare", lambda a, b: {**real(a, b), "tables": [{"table": "x", "match": False}]})
    with pytest.raises(BackupError, match="previous data was put back"):
        backup.restore_backup(settings.database_url, Path(settings.backup_dir) / name, settings.backup_dir)
    assert backup.fingerprint(db_file(settings)) == damaged  # nothing was lost by the failed attempt


@pytest.mark.req("AC10", "NFR01")
def test_bad_backups_are_refused_and_the_live_data_is_left_alone(client, lib, admin_h, settings):
    make_library(client, lib)
    folder = Path(settings.backup_dir)
    good = create(client, admin_h)["name"]
    (folder / "garbage.db").write_bytes(b"not a database" * 100)
    newer = folder / "newer.db"
    (folder / good).replace(newer)
    con = sqlite3.connect(newer)
    con.execute("UPDATE alembic_version SET version_num = '9999'")
    con.commit()
    con.close()
    live = backup.fingerprint(db_file(settings))
    for name, words in (("garbage.db", "damaged"), ("newer.db", "newer version")):
        r = restore(client, admin_h, name)
        assert r.status_code == 409 and words in r.json()["detail"]["message"]
    assert backup.fingerprint(db_file(settings)) == live
    assert not [p for p in folder.glob("pre-restore-*.db")]  # refused before even taking the safety copy


@pytest.mark.req("AC10", "FR12")
def test_a_backup_from_an_older_version_is_restored_and_brought_up_to_date(client, lib, admin_h, settings, tmp_path):
    old_url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    migrate.upgrade(old_url, "0006")  # the schema as it was before fines and the gate
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute(
        "INSERT INTO members (member_no, name, status, fines_cents, created_at) "
        "VALUES ('OLD1','Old Member','active',0,'2026-01-01')"
    )
    con.commit()
    con.close()
    info = backup.create_backup(old_url, settings.backup_dir, "from-the-past")
    listed = client.get("/api/backups", headers=admin_h).json()["backups"]
    assert listed[0]["revision"] == "0006"
    v = client.get(f"/api/backups/{info.path.name}/verify", headers=admin_h).json()
    assert v["ok"] and v["needs_upgrade"] is True
    admin_token = login_card(client, "STAFFADMIN01")  # a session made now will not exist in the old backup
    r = restore(client, admin_h, info.path.name)
    assert r.status_code == 200 and r.json()["upgraded_to"] == backup.head()
    assert sql(settings, "SELECT version_num FROM alembic_version") == [(backup.head(),)]
    assert sql(settings, "SELECT name FROM members") == [("Old Member",)]  # the old data, in the new schema
    assert sql(settings, "SELECT COUNT(*) FROM fine_entries") == [(0,)]  # the tables added since exist again, empty
    assert (
        client.get("/api/backups", headers=admin_token).status_code == 401
    )  # the session was in the newer data: sign in again


@pytest.mark.req("AC10", "NFR02")
def test_only_plain_backup_names_are_accepted(client, admin_h, settings):
    create(client, admin_h)
    for bad in ("..%2f..%2faisys.db", "a%2fb.db", "..%5cx.db", "nothere.db", "x.txt", "%2e%2e%2f%2e%2e%2fetc"):
        for path in ("verify", "compare"):
            assert client.get(f"/api/backups/{bad}/{path}", headers=admin_h).status_code in (404, 405), bad
        assert restore(client, admin_h, bad).status_code in (404, 405), bad


@pytest.mark.req("AC10", "NFR02")
def test_backups_are_for_administrators_only(client, lib, admin_h):
    name = create(client, admin_h)["name"]
    anon = TestClient(client.app)
    assert anon.get("/api/backups").status_code == 401
    for hdr in (lib, login_card(client, "STAFFCLERK01"), login_card(client, "STAFFAUDIT01")):
        assert client.get("/api/backups", headers=hdr).status_code == 403
        assert client.post("/api/backups", headers=hdr, json={}).status_code == 403
        assert restore(client, hdr, name).status_code == 403
        assert client.post("/api/backups/mock/drill", headers=hdr).status_code == 403
    assert client.get("/backups", headers=lib).status_code == 403
    page = client.get("/backups", headers=admin_h).text
    assert "Back up, check, restore" in page and "Disaster drill" in page and "Type <b>RESTORE</b>" in page


# ================================================================== the drills (development only)
@pytest.mark.req("AC10", "NFR01")
def test_the_full_drill_shows_the_damage_then_the_proof(client, lib, admin_h, settings):
    make_library(client, lib)
    d = client.post("/api/backups/mock/drill", headers=admin_h).json()
    assert d["backup"].startswith("before-drill-") and d["damage"]["rows_touched"] > 0
    assert d["after_damage"]["data_intact"] is False and "members" in d["after_damage"]["differing"]
    assert d["restored"]["proof"]["data_intact"] is True and len(d["restored"]["proof"]["tables"]) > 20
    assert (Path(settings.backup_dir) / d["backup"]).is_file()
    assert client.get("/api/catalogue/items", headers=lib).status_code == 200  # still working afterwards


@pytest.mark.req("AC10", "AC01", "NFR01")
def test_a_failed_import_leaves_everything_unchanged_and_its_backup_proves_it(client, lib, admin_h, settings):
    base = upload(client, lib, sample_csv(40)[0]).json()
    client.post(f"/api/migrations/{base['id']}/commit", headers=lib)  # some existing data
    data, _, _, _ = sample_csv(60, seed=7)
    batch = upload(client, lib, data).json()
    assert client.post("/api/migrations/mock/fail-next-commit", headers=lib).status_code == 200
    failed = client.post(f"/api/migrations/{batch['id']}/commit", headers=lib)
    assert failed.status_code == 500 and "unchanged" in failed.json()["detail"]
    kept = client.get(f"/api/migrations/{batch['id']}", headers=lib).json()
    name = Path(kept["summary"]["backup_path"]).name
    shutil_target = Path(settings.backup_dir) / name
    assert shutil_target.is_file()  # the pre-migration backup is kept
    proof = compare(client, admin_h, name)
    assert proof["library_data_intact"] is True  # no library data changed: nothing leaked in from the failed import
    assert proof["differing"] == ["migration_batches"]  # the import's own log, which now says "failed"
    status = {t["table"]: t["status"] for t in proof["tables"]}
    assert status["items"] == status["bib_records"] == status["members"] == status["rfid_tags"] == "same"
    assert client.post(f"/api/migrations/{batch['id']}/commit", headers=lib).status_code in (
        200,
        409,
    )  # fault disarmed itself
    assert client.delete("/api/migrations/mock/fail-next-commit", headers=lib).json() == {"armed": False}


@pytest.mark.req("AC10", "NFR02")
def test_the_drills_do_not_exist_in_production(settings):
    prod = dataclasses.replace(settings, environment="production", secret_key="k" * 40)
    app = create_app(prod)
    seed_demo(prod)
    with TestClient(app, base_url="https://testserver") as c:  # the session cookie is Secure in production
        hdr = login_card(c, "STAFFADMIN01")
        assert c.post("/api/backups/mock/drill", headers=hdr).status_code == 404
        assert c.post("/api/backups/mock/damage", headers=hdr).status_code == 404
        assert c.post("/api/migrations/mock/fail-next-commit", headers=hdr).status_code == 404
        assert "Disaster drill" not in c.get("/backups", headers=hdr).text
        assert c.get("/api/backups", headers=hdr).status_code == 200  # the real tools stay


# ================================================================== the command line
@pytest.mark.req("AC10", "NFR01")
def test_the_command_line_can_back_up_verify_compare_and_restore(tmp_path, capsys):
    db = tmp_path / "cli.db"
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[database]\nurl = "sqlite:///{db.as_posix()}"\n[backup]\ndir = "{(tmp_path / "bk").as_posix()}"\n',
        encoding="utf-8",
    )
    assert cli_main(["--config", str(cfg), "seed-demo"]) == 0

    def run(*args):
        capsys.readouterr()
        code = cli_main(["--config", str(cfg), "backup", *args])
        return code, capsys.readouterr().out

    code, text = run("create", "--label", "cli")
    assert code == 0 and "backup created" in text
    code, text = run("list")
    name = next(p.name for p in (tmp_path / "bk").glob("cli-*.db"))
    assert code == 0 and name in text and "1 backup(s)" in text
    assert run("verify", name)[0] == 0 and "healthy" in run("verify", name)[1]
    assert run("compare", name)[0] == 0 and "identical" in run("compare", name)[1]
    con = sqlite3.connect(db)
    con.execute("UPDATE members SET fines_cents = 999")
    con.commit()
    con.close()
    code, text = run("compare", name)
    assert code == 1 and "DIFFERS in: members" in text
    code, text = run("restore", name)  # without --yes it only explains
    assert code == 2 and "Stop the application first" in text
    assert sqlite3.connect(db).execute("SELECT MAX(fines_cents) FROM members").fetchone() == (999,)  # untouched
    code, text = run("restore", name, "--yes")
    assert code == 0 and "tables match the backup" in text
    assert sqlite3.connect(db).execute("SELECT MAX(fines_cents) FROM members").fetchone() == (
        1250,
    )  # the demo's 12.50 is back
    assert run("verify", "no-such-file.db")[0] == 1
    assert "backup.restore" in {a for (a,) in sqlite3.connect(db).execute("SELECT action FROM audit_events")}


@pytest.mark.req("AC10", "NFR01")
def test_backups_do_not_leak_into_the_real_folder(settings):
    assert (
        Path(settings.backup_dir) != Path("data/backups") and "backups" in settings.backup_dir
    )  # tests use a temp folder


@pytest.mark.req("AC10")
def test_the_backups_page_is_in_the_menu_for_admins(client, admin_h, lib):
    assert 'href="/backups"' in client.get("/dashboard", headers=admin_h).text
    assert 'href="/backups"' not in client.get("/dashboard", headers=lib).text
    book(client, lib, "ZZ1")
    assert client.get("/backups", headers=admin_h).status_code == 200


# ================================================================== found while testing at scale
@pytest.mark.req("AC10", "NFR06")
def test_one_broken_manifest_never_breaks_the_list(client, admin_h, settings):
    folder = Path(settings.backup_dir)
    names = [create(client, admin_h, f"m{n}")["name"] for n in range(4)]
    bad = [
        "{ this is not json",
        '{"tables": "oops", "sha256": 5, "label": 7, "created_at": [1], "revision": {"x": 1}}',
        '["a", "list"]',
        '{"tables": {"members": "many", "items": 5}}',
    ]
    for name, text in zip(names, bad, strict=True):
        (folder / (name + ".json")).write_text(text, encoding="utf-8")
    r = client.get("/api/backups", headers=admin_h)
    assert r.status_code == 200 and r.json()["total"] == 4  # every one is still listed
    for name in names:
        v = client.get(f"/api/backups/{name}/verify", headers=admin_h)
        assert v.status_code == 200  # and can be checked; a wrong checksum in the manifest is flagged, never trusted
    assert client.get(f"/api/backups/{names[1]}/verify", headers=admin_h).json()["checksum_matches"] is False


@pytest.mark.req("AC10", "NFR02")
def test_files_with_awkward_names_are_reported_not_listed(client, admin_h, settings):
    good = create(client, admin_h)["name"]
    folder = Path(settings.backup_dir)
    for odd in ("my backup.db", "semi;colon.db", "quote'd.db", "tilde~.db"):
        (folder / odd).write_bytes((folder / good).read_bytes())
    r = client.get("/api/backups", headers=admin_h).json()
    assert [b["name"] for b in r["backups"]] == [good]  # nothing is offered that the actions would reject
    assert sorted(r["skipped"]) == ["my backup.db", "quote'd.db", "semi;colon.db", "tilde~.db"]
    assert client.get("/api/backups/my%20backup.db/verify", headers=admin_h).status_code == 404


@pytest.mark.req("AC10")
def test_the_backup_list_is_paged(client, admin_h, settings):
    names = [create(client, admin_h, f"p{n:02d}")["name"] for n in range(7)]
    first = client.get("/api/backups?limit=3", headers=admin_h).json()
    assert first["total"] == 7 and len(first["backups"]) == 3 and first["limit"] == 3 and first["offset"] == 0
    assert [b["name"] for b in first["backups"]] == names[::-1][:3]  # newest first
    last = client.get("/api/backups?limit=3&offset=6", headers=admin_h).json()
    assert [b["name"] for b in last["backups"]] == [names[0]]
    assert client.get("/api/backups?limit=0&offset=-5", headers=admin_h).json()["limit"] == 1  # clamped, no crash
    assert client.get("/api/backups?limit=9999", headers=admin_h).json()["limit"] == 200
    assert 'id="pager"' in client.get("/backups", headers=admin_h).text


@pytest.mark.req("AC10", "NFR06")
def test_a_restore_gives_up_cleanly_when_the_database_stays_busy(client, lib, admin_h, settings, monkeypatch):
    import threading
    import time

    make_library(client, lib)
    name = create(client, admin_h)["name"]
    client.post("/api/backups/mock/damage", headers=admin_h)
    damaged = backup.fingerprint(db_file(settings))
    monkeypatch.setattr(backup, "RESTORE_WAIT_S", 1.0)  # do not make the test wait 20 seconds
    holding, release = threading.Event(), threading.Event()

    def hold():
        con = sqlite3.connect(db_file(settings), timeout=1)
        con.execute("BEGIN EXCLUSIVE")  # another process is in the middle of writing
        holding.set()
        release.wait(15)
        con.rollback()
        con.close()

    t = threading.Thread(target=hold)
    t.start()
    holding.wait()
    started = time.monotonic()
    r = restore(client, admin_h, name)
    took = time.monotonic() - started
    release.set()
    t.join()
    assert r.status_code == 409 and "stayed busy" in r.json()["detail"]["message"] and "nothing was changed" in r.text
    assert took < 8  # it gave up; it did not hang the request
    assert backup.fingerprint(db_file(settings)) == damaged  # the live data was not touched
    assert restore(client, admin_h, name).status_code == 200  # and once the lock is gone the restore works


@pytest.mark.req("AC10", "NFR06")
def test_a_brief_lock_is_waited_out(client, lib, admin_h, settings, monkeypatch):
    import threading

    make_library(client, lib)
    name = create(client, admin_h)["name"]
    client.post("/api/backups/mock/damage", headers=admin_h)
    monkeypatch.setattr(backup, "RESTORE_WAIT_S", 10.0)
    holding = threading.Event()

    def hold():
        con = sqlite3.connect(db_file(settings), timeout=1)
        con.execute("BEGIN EXCLUSIVE")
        holding.set()
        threading.Event().wait(1.5)  # a short write elsewhere
        con.rollback()
        con.close()

    t = threading.Thread(target=hold)
    t.start()
    holding.wait()
    assert restore(client, admin_h, name).status_code == 200  # waited, then restored
    t.join()
    assert compare(client, admin_h, name)["data_intact"] is True


@pytest.mark.req("AC10", "NFR02")
def test_any_label_becomes_a_plain_file_name_inside_the_folder(client, admin_h, settings):
    folder = Path(settings.backup_dir).resolve()
    for label in ("../../evil", "ünï-cødé ✓", "a/b\\c:d*e?f", "<script>x</script>", "   ", "日本語", "x" * 40):
        b = create(client, admin_h, label)
        assert backup.usable_name(b["name"]) and (folder / b["name"]).resolve().parent == folder, label
        assert (
            client.get(f"/api/backups/{b['name']}/verify", headers=admin_h).json()["ok"] is True
        )  # and it can be used
    assert client.get("/api/backups", headers=admin_h).json()["skipped"] == []


# ================================================================== nobody else touches the data during a restore
@pytest.mark.req("AC10", "NFR01")
def test_everyone_else_is_told_to_wait_while_a_restore_runs(client, lib, admin_h):
    client.app.state.maintenance.set()  # what a running restore does
    try:
        for path in ("/api/catalogue/items", "/api/reports/members", "/dashboard", "/api/backups"):
            r = client.get(path, headers=lib)
            assert r.status_code == 503 and r.headers["retry-after"] == "5", path
        r = client.post("/api/circulation/checkout", headers=lib, json={"member_no": "M0001", "item_ref": "X"})
        assert r.status_code == 503 and "being restored" in r.json()["detail"]["message"]  # writes are refused too
        assert restore(client, admin_h, "any.db").status_code == 503  # and a second restore cannot start
        assert client.get("/static/app.css").status_code == 200  # the page's own files still load
    finally:
        client.app.state.maintenance.clear()
    assert client.get("/api/catalogue/items", headers=lib).status_code == 200  # back to normal


@pytest.mark.req("AC10", "NFR01")
def test_maintenance_mode_is_always_switched_off_again(client, lib, admin_h, settings):
    make_library(client, lib)
    folder = Path(settings.backup_dir)
    good = create(client, admin_h)["name"]
    (folder / "garbage.db").write_bytes(b"x" * 500)
    assert restore(client, admin_h, "garbage.db").status_code == 409  # a refused restore...
    assert not client.app.state.maintenance.is_set()
    assert client.get("/api/catalogue/items", headers=lib).status_code == 200  # ...leaves the app working
    assert restore(client, admin_h, good).status_code == 200  # a good one too
    assert not client.app.state.maintenance.is_set()
    assert client.get("/api/catalogue/items", headers=lib).status_code == 200
