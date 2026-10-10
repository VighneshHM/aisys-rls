"""The full demo library (`seed-demo`): every menu has something in it, built through the application's own API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aisys.app import create_app
from aisys.cli import main as cli_main
from aisys.db import make_engine, make_sessionmaker
from aisys.devtools import demo_seed
from aisys.models import AuditEvent, Item, Loan, Member, RfidTag, User
from aisys.timeutil import aware
from conftest import login_card


@pytest.fixture(scope="module")
def demo(tmp_path_factory, migrated_db):
    """One demo library for the whole module (it takes a few seconds to replay)."""
    import shutil

    from aisys.config import Settings

    folder = tmp_path_factory.mktemp("demo")
    shutil.copyfile(migrated_db, folder / "demo.db")
    settings = Settings(database_url=f"sqlite:///{(folder / 'demo.db').as_posix()}", backup_dir=str(folder / "backups"))
    counts = demo_seed.seed_full_demo(settings, quiet=True)
    return settings, counts


@pytest.fixture
def client(demo):
    settings, _ = demo
    with TestClient(create_app(settings)) as c:
        yield c


def _db(settings):
    return make_sessionmaker(make_engine(settings.database_url))()


def test_the_numbers_asked_for(demo):
    settings, counts = demo
    assert 250 <= counts["items"] <= 300, counts
    assert 10 <= counts["untagged"] <= 25, "a few books, all outside the imported 200, have no tag"
    assert counts["patrons"] == 20
    with _db(settings) as db:
        roles = dict(db.execute(select(User.role_id, func.count()).group_by(User.role_id)).all())
        by_name = {u.role.name for u in db.scalars(select(User))}
        assert sorted(roles.values()) == [1, 1, 2, 3] and by_name == {
            "admin",
            "librarian",
            "circulation_clerk",
            "auditor",
        }
        imported = set(db.scalars(select(Item.id).where(Item.accession_no.like("ACC0%"))))
        tagged = set(db.scalars(select(RfidTag.item_id).where(RfidTag.status == "active")))
        assert len(imported) == 191 and imported <= tagged, "every book from the 200-row spreadsheet is tagged"


def test_the_sign_in_page_still_offers_one_card_of_each_kind(client):
    page = client.get("/").text
    for uid in ("STAFFADMIN01", "STAFFLIB0001", "STAFFCLERK01", "STAFFAUDIT01", "PATRON000001"):
        assert uid in page
    for uid in ("STAFFLIB0002", "STAFFCLERK02", "STAFFCLERK03"):
        assert uid not in page, "the extra staff exist but the sign-in page keeps one of each role"


def test_patrons_have_different_situations_and_histories(demo):
    settings, _ = demo
    now = datetime.now(UTC)
    with _db(settings) as db:
        members = {m.member_no: m for m in db.scalars(select(Member))}
        assert members["M0003"].status == "blocked" and members["M0004"].fines_cents > 500
        assert members["M0018"].removed_at is not None, "one patron moved away and was removed"
        loans = db.scalars(select(Loan)).all()
        past = [loan for loan in loans if loan.returned_at is not None]
        current = [loan for loan in loans if loan.returned_at is None]
        assert len(past) >= 30 and len(current) >= 20
        assert min(aware(loan.out_at) for loan in loans) < now - timedelta(days=80), "history goes back months"
        assert any(aware(loan.due_at) < now for loan in current), "some current loans are overdue"
        assert any(loan.renewals for loan in loans)
        assert len({loan.member_id for loan in past}) >= 10, "many patrons have borrowed and returned before"
        assert max(aware(e.ts) for e in db.scalars(select(AuditEvent))) <= now, "nothing is dated in the future"
        actors = set(db.scalars(select(AuditEvent.actor)))
        assert {"admin", "librarian", "noor", "clerk", "ravi", "jonas", "auditor"} <= actors, (
            "every member of staff works"
        )


@pytest.mark.parametrize(
    ("card", "path", "has"),
    [
        ("STAFFLIB0001", "/api/catalogue/items", lambda r: r["total"] >= 250),
        ("STAFFCLERK01", "/api/circulation/events", lambda r: len(r["events"]) >= 20),
        ("STAFFLIB0001", "/api/members", lambda r: r["total"] == 19),  # the removed patron is listed only on request
        ("STAFFLIB0001", "/api/rfid/tags", lambda r: r["total"] >= 240),
        ("STAFFLIB0001", "/api/inventory/sessions", lambda r: {s["status"] for s in r} == {"open", "closed"}),
        ("STAFFLIB0001", "/api/shelves", lambda r: len(r["shelves"]) >= 6),
        ("STAFFLIB0001", "/api/labels/jobs", lambda r: len(r["jobs"]) >= 2),
        ("STAFFLIB0001", "/api/acquisitions", lambda r: len({o["status"] for o in r["orders"]}) == 5),
        ("STAFFLIB0001", "/api/serials", lambda r: len(r["serials"]) == 3),
        ("STAFFLIB0001", "/api/gate/events", lambda r: any(e["alarm"] for e in r["events"])),
        ("STAFFADMIN01", "/api/notifications", lambda r: r["total"] >= 20),
        ("STAFFLIB0001", "/api/migrations", lambda r: len(r) == 3),
        ("STAFFAUDIT01", "/api/audit/events", lambda r: r["total"] >= 500),
        ("STAFFADMIN01", "/api/backups", lambda r: r["total"] >= 1),
        ("STAFFADMIN01", "/api/admin/people", lambda r: len(r["people"]) == 7),
        ("STAFFAUDIT01", "/api/reports/circulation", lambda r: len(r.get("rows", [])) >= 20),
        ("STAFFAUDIT01", "/api/reports/fines", lambda r: len(r.get("rows", [])) >= 3),
    ],
)
def test_every_menu_has_something_in_it(client, card, path, has):
    r = client.get(path, headers=login_card(client, card))
    assert r.status_code == 200, r.text
    assert has(r.json()), path


def test_the_settings_page_shows_a_change_and_the_registry_every_tag_state(client):
    admin = login_card(client, "STAFFADMIN01")
    items = {i["key"]: i for i in client.get("/api/admin/settings", headers=admin).json()["items"]}
    assert items["circulation.max_renewals"]["value"] == 3
    lib = login_card(client, "STAFFLIB0001")
    for state in ("active", "removed", "retired"):
        assert client.get("/api/rfid/tags", params={"state": state}, headers=lib).json()["total"] >= 1, state


def test_every_staff_page_opens_with_the_demo_data(client):
    login_card(client, "STAFFADMIN01")
    for path in ("/dashboard", "/catalogue", "/circulation", "/members", "/tagging", "/tags", "/inventory", "/shelves",
                 "/labels", "/acquisitions", "/serials", "/gate", "/reports", "/migration", "/audit", "/backups",
                 "/updates", "/settings", "/admin"):  # fmt: skip
        assert client.get(path).status_code == 200, path


def test_it_goes_only_into_an_empty_catalogue(demo, capsys, tmp_path):
    settings, _ = demo
    with pytest.raises(demo_seed.DemoSeedError, match="already has items"):
        demo_seed.seed_full_demo(settings, quiet=True)
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[database]\nurl = "{settings.database_url}"\n', encoding="utf-8")
    assert cli_main(["--config", str(cfg), "seed-demo"]) == 0
    assert "already has items" in capsys.readouterr().out


def test_the_minimal_seed_is_what_the_tests_use(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[database]\nurl = "sqlite:///{(tmp_path / "m.db").as_posix()}"\n', encoding="utf-8")
    assert cli_main(["--config", str(cfg), "seed-demo", "--minimal"]) == 0
    from aisys.config import load_settings

    assert demo_seed.catalogue_size(load_settings(cfg)) == 0
    capsys.readouterr()
