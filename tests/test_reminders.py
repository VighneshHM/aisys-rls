"""FR09: patron loan reminders (a day before the due date and on it), the delivery loop, member contact details."""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aisys import migrate
from aisys.app import create_app
from aisys.cli import main as cli_main
from aisys.models import Loan, Member, Notification
from aisys.notifications import queue, reminders, runner
from aisys.notifications.providers import MockProvider, NotificationConfig, build_providers
from conftest import login_card
from test_tagging import make_item

pytestmark = pytest.mark.req("FR09")

NOON = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # "today" in these tests


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


@pytest.fixture(autouse=True)
def blank_contacts(client):
    """The demo data gives a few patrons synthetic contact details; these tests start from members with none."""
    with client.app.state.session_factory() as s:
        for m in s.scalars(select(Member)):
            m.email = m.phone = ""
        s.commit()


@pytest.fixture
def db(client):
    with client.app.state.session_factory() as session:
        yield session


def member(db, member_no="M0001", **fields):
    m = db.scalar(select(Member).where(Member.member_no == member_no))
    for k, v in fields.items():
        setattr(m, k, v)
    db.commit()
    return m


def make_loan(client, lib, db, accession, due_at, member_no="M0001", returned=None) -> int:
    item = make_item(client, lib, accession, f"Title of {accession}")
    m = db.scalar(select(Member).where(Member.member_no == member_no))
    loan = Loan(member_id=m.id, item_id=item["id"], due_at=due_at, out_by="librarian", returned_at=returned)
    db.add(loan)
    db.commit()
    return loan.id


def day(offset: int, hour: int = 17) -> datetime:
    d = NOON + timedelta(days=offset)
    return d.replace(hour=hour, minute=0)


def scan(db, cfg=None, now=NOON):
    return reminders.scan(db, cfg or NotificationConfig(), library_name="Test Library", now=now)


def queued(db):
    return list(db.scalars(select(Notification).where(Notification.loan_id.is_not(None)).order_by(Notification.id)))


# ---------- which loans get a reminder, and when ----------
def test_one_day_before_and_on_the_due_day_only(client, lib, db):
    member(db, email="priya@example.org")
    tomorrow = make_loan(client, lib, db, "R-TOM", day(1))
    today = make_loan(client, lib, db, "R-TOD", day(0))
    make_loan(client, lib, db, "R-2D", day(2))  # two days away: not yet
    make_loan(client, lib, db, "R-LATE", day(-1))  # already overdue: the reminders are over
    make_loan(client, lib, db, "R-BACK", day(1), returned=NOON)  # returned: nothing to remind
    r = scan(db)
    assert r["queued"] == 2 and r["due_soon"] == 1 and r["due_today"] == 1
    kinds = {n.loan_id: n.kind for n in queued(db)}
    assert kinds == {tomorrow: "due_soon", today: "due_today"}


def test_day_boundaries_use_whole_utc_days(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-EDGE", datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    late_evening = datetime(2026, 10, 9, 23, 59, tzinfo=UTC)
    assert scan(db, now=late_evening)["due_soon"] == 1  # due 30 minutes after midnight: still "tomorrow"
    just_after = datetime(2026, 10, 10, 0, 1, tzinfo=UTC)
    assert scan(db, now=just_after)["due_today"] == 1  # a minute into the due day: "today"
    assert [n.kind for n in queued(db)] == ["due_soon", "due_today"]


def test_the_number_of_days_before_is_configurable(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-3", day(3))
    assert scan(db)["queued"] == 0
    cfg = NotificationConfig(remind_days_before=3)
    assert scan(db, cfg)["due_soon"] == 1
    assert "due in 3 days" in queued(db)[0].subject


def test_reminders_can_be_switched_off_in_config(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-OFF", day(0))
    assert scan(db, NotificationConfig(reminders=False)) == {
        "queued": 0, "due_soon": 0, "due_today": 0, "already_sent": 0, "no_contact": 0, "opted_out": 0,
    }  # fmt: skip
    assert queued(db) == []


# ---------- who is reached, and how ----------
def test_a_member_is_reached_on_each_channel_they_have(client, lib, db):
    member(db, email="priya@example.org", phone="+441234567890")
    make_loan(client, lib, db, "R-BOTH", day(0))
    assert scan(db)["queued"] == 2
    assert {(n.channel, n.recipient) for n in queued(db)} == {("email", "priya@example.org"), ("sms", "+441234567890")}


def test_no_contact_and_opt_out_are_counted_not_sent(client, lib, db):
    make_loan(client, lib, db, "R-NC", day(0))
    r = scan(db)
    assert r["queued"] == 0 and r["no_contact"] == 1
    member(db, email="priya@example.org", reminders_on=False)
    r = scan(db)
    assert r["queued"] == 0 and r["opted_out"] == 1 and queued(db) == []


def test_a_bad_stored_address_is_skipped_not_fatal(client, lib, db):
    member(db, email="not-an-address", phone="+441234567890")  # e.g. imported or edited by hand
    make_loan(client, lib, db, "R-BAD", day(0))
    assert scan(db)["queued"] == 1 and queued(db)[0].channel == "sms"


def test_blocked_members_still_get_their_reminders(client, lib, db):
    member(db, "M0003", email="lena@example.org")  # M0003 is blocked in the demo data
    make_loan(client, lib, db, "R-BLK", day(0), member_no="M0003")
    assert scan(db)["queued"] == 1


# ---------- once only ----------
def test_scanning_again_never_repeats_a_reminder(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-ONCE", day(0))
    assert scan(db)["queued"] == 1
    for _ in range(3):
        again = scan(db)
        assert again["queued"] == 0 and again["already_sent"] == 1
    assert len(queued(db)) == 1


def test_two_schedulers_at_once_still_send_one(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-RACE", day(0))
    factory = client.app.state.session_factory
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        with factory() as s:
            scan(s)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    db.expire_all()
    assert len(queued(db)) == 1, "the database's unique index decides, whoever gets there first"


def test_a_renewal_gets_fresh_reminders(client, lib, db):
    member(db, email="priya@example.org")
    loan_id = make_loan(client, lib, db, "R-REN", day(0))
    scan(db)
    loan = db.get(Loan, loan_id)
    loan.due_at = day(14)  # renewed for two weeks
    db.commit()
    later = NOON + timedelta(days=13)
    assert scan(db, now=later)["due_soon"] == 1
    assert [n.due_on for n in queued(db)] == [str(day(0).date()), str(day(14).date())]


# ---------- the words ----------
def test_messages_say_what_is_due_and_when(client, lib, db):
    member(db, email="priya@example.org", phone="+441234567890")
    make_loan(client, lib, db, "R-WORDS", day(1))
    scan(db)
    email = next(n for n in queued(db) if n.channel == "email")
    sms = next(n for n in queued(db) if n.channel == "sms")
    assert email.subject == "Reminder: 'Title of R-WORDS' is due tomorrow"
    assert "Hello Priya" in email.body and "R-WORDS" in email.body and "10 Oct 2026" in email.body
    assert "Test Library" in email.body
    assert len(sms.body) <= 160 and "due tomorrow" in sms.body and "Title of R-WORDS" in sms.body
    make_loan(client, lib, db, "R-TODAY", day(0))
    scan(db)
    assert any("is due today" in n.subject for n in queued(db))


# ---------- delivery ----------
def test_reminders_are_delivered_through_the_provider_adapters(client, lib, db):
    member(db, email="priya@example.org", phone="+441234567890")
    make_loan(client, lib, db, "R-SEND", day(0))
    providers = build_providers(NotificationConfig())
    out = runner.run_once(client.app.state.session_factory, providers, NotificationConfig(), now=NOON)
    assert out["reminders"]["queued"] == 2 and out["delivery"]["sent"] == 2
    boxes = {c: p.outbox for c, p in providers.items() if isinstance(p, MockProvider)}
    assert boxes["email"][0]["to"] == "priya@example.org" and "due today" in boxes["email"][0]["subject"]
    assert boxes["sms"][0]["to"] == "+441234567890"
    again = runner.run_once(client.app.state.session_factory, providers, NotificationConfig(), now=NOON)
    assert again["reminders"]["queued"] == 0 and again["delivery"]["tried"] == 0


def test_a_reminder_for_a_returned_item_is_cancelled_not_sent(client, lib, db):
    member(db, email="priya@example.org")
    loan_id = make_loan(client, lib, db, "R-RET", day(0))
    scan(db)
    loan = db.get(Loan, loan_id)
    loan.returned_at = NOON
    db.commit()
    providers = build_providers(NotificationConfig())
    result = queue.dispatch(db, providers, NotificationConfig(), now=NOON)
    assert result["cancelled"] == 1 and result["sent"] == 0
    n = queued(db)[0]
    assert n.status == "cancelled" and "returned" in n.last_error
    assert providers["email"].outbox == []


def test_a_reminder_for_a_renewed_loan_is_cancelled(client, lib, db):
    member(db, email="priya@example.org")
    loan_id = make_loan(client, lib, db, "R-RENEWED", day(0))
    scan(db)
    db.get(Loan, loan_id).due_at = day(14)
    db.commit()
    result = queue.dispatch(db, build_providers(NotificationConfig()), NotificationConfig(), now=NOON)
    assert result["cancelled"] == 1 and "renewed" in queued(db)[0].last_error


def test_a_failing_provider_retries_the_reminder(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-FAIL", day(0))
    scan(db)
    cfg = NotificationConfig(backoff_s=0.0)
    providers = build_providers(cfg)
    providers["email"].fail_next("error", 1)
    first = queue.dispatch(db, providers, cfg, now=NOON)
    second = queue.dispatch(db, providers, cfg, now=NOON + timedelta(seconds=1))
    assert first["retry_later"] == 1 and second["sent"] == 1


# ---------- the loop ----------
def test_the_loop_runs_passes_survives_errors_and_stops():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database hiccup")
        return {"reminders": {"queued": 0}, "delivery": {"sent": 0}}

    loop = runner.Loop(flaky, 0.05).start()
    deadline = time.time() + 5
    while len(calls) < 4 and time.time() < deadline:
        time.sleep(0.02)
    loop.stop()
    assert len(calls) >= 4 and loop.errors == 1 and loop.passes >= 3, "one bad pass does not stop the loop"
    n = len(calls)
    time.sleep(0.2)
    assert len(calls) == n and not loop.thread.is_alive(), "stopped means stopped"


def test_the_loop_does_nothing_while_paused():
    paused = threading.Event()
    paused.set()
    calls = []
    loop = runner.Loop(lambda: calls.append(1) or {}, 0.02, paused=paused.is_set).start()
    time.sleep(0.2)
    assert calls == []
    paused.clear()
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    loop.stop()
    assert calls


def test_the_running_app_delivers_reminders_by_itself(settings, tmp_path):
    cfg = dataclasses.replace(settings, notifications=NotificationConfig(auto_dispatch_s=0.1, backoff_s=0.0))
    app = create_app(cfg)
    from aisys.cli import seed_demo

    seed_demo(cfg)
    with TestClient(app) as c:
        assert app.state.notify_loop is not None
        lib_h = login_card(c, "STAFFLIB0001")
        with app.state.session_factory() as s:
            member(s, email="priya@example.org")
            make_loan(c, lib_h, s, "R-AUTO", datetime.now(UTC).replace(hour=23, minute=0))
        deadline = time.time() + 8
        sent = []
        while not sent and time.time() < deadline:
            time.sleep(0.1)
            sent = app.state.providers["email"].outbox
        assert sent and sent[0]["to"] == "priya@example.org"
        assert len(sent) == 1, "delivered once, however many passes ran"
    assert not app.state.notify_loop.thread.is_alive(), "stopped with the application"


def test_no_loop_when_not_configured(client):
    with TestClient(client.app):
        assert client.app.state.notify_loop is None


# ---------- command line ----------
def test_cli_one_pass_and_bad_interval(settings, client, capsys):
    path = settings.database_url.removeprefix("sqlite:///")
    assert path
    import os

    os.environ["AISYS_DATABASE_URL"] = settings.database_url
    try:
        assert cli_main(["notify-dispatch"]) == 0
        out = capsys.readouterr().out
        assert "notifications:" in out and "'reminders'" in out and "'delivery'" in out
        assert cli_main(["notify-dispatch", "--every", "0.2"]) == 2
        assert "at least 1 second" in capsys.readouterr().out
    finally:
        os.environ.pop("AISYS_DATABASE_URL", None)


# ---------- member contact details ----------
def test_contact_can_be_set_and_cleared(client, lib):
    r = client.patch(
        "/api/circulation/members/M0001/contact",
        json={"email": " Priya@Example.org ", "phone": "+44 (1234) 567-890", "reminders_on": True},
        headers=lib,
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact"] == {"email": "Priya@Example.org", "phone": "+441234567890", "reminders_on": True}
    cleared = client.patch("/api/circulation/members/M0001/contact", json={"email": "", "phone": ""}, headers=lib)
    assert cleared.json()["contact"]["email"] == "" and cleared.json()["contact"]["phone"] == ""
    off = client.patch("/api/circulation/members/M0001/contact", json={"reminders_on": False}, headers=lib)
    assert off.json()["contact"]["reminders_on"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"email": "nope"},
        {"email": "a@b"},
        {"email": "x" * 300 + "@example.org"},
        {"phone": "12"},
        {"phone": "abc"},
        {"phone": "+" + "1" * 20},
    ],
)
def test_bad_contact_details_are_refused(client, lib, body):
    r = client.patch("/api/circulation/members/M0001/contact", json=body, headers=lib)
    assert r.status_code in (422, 409)
    assert client.get("/api/circulation/members/M0001", headers=lib).json()["contact"]["email"] == ""


def test_only_staff_who_can_edit_members_change_contact(client, lib):
    clerk = login_card(client, "STAFFCLERK01")
    assert (
        client.patch(
            "/api/circulation/members/M0001/contact", json={"email": "a@example.org"}, headers=clerk
        ).status_code
        == 403
    )
    assert client.get("/api/circulation/members/M0001", headers=clerk).status_code == 200
    client.cookies.clear()
    assert client.patch("/api/circulation/members/M0001/contact", json={"email": "a@example.org"}).status_code == 401
    assert (
        client.patch("/api/circulation/members/NOPE/contact", json={"email": "a@example.org"}, headers=lib).status_code
        == 404
    )


def test_the_audit_log_records_the_change_but_not_the_address(client, lib):
    client.patch(
        "/api/circulation/members/M0001/contact",
        json={"email": "secret.person@example.org", "phone": "+441234567890"},
        headers=lib,
    )
    aud = login_card(client, "STAFFAUDIT01")
    events = client.get("/api/audit/events?action=member.contact", headers=aud).json()["events"]
    assert events and events[0]["detail"] == {"email": "set", "phone": "set"}
    assert "secret.person" not in str(events) and "441234567890" not in str(events)


def test_a_new_member_can_be_created_with_contact_details(client, lib):
    r = client.post(
        "/api/circulation/members",
        json={"name": "Dana Reyes", "email": "dana@example.org", "phone": "+15551234567"},
        headers=lib,
    )
    assert r.status_code == 201 and r.json()["contact"]["email"] == "dana@example.org"
    bad = client.post("/api/circulation/members", json={"name": "Eve Hall", "email": "bad"}, headers=lib)
    assert bad.status_code == 422


def test_the_member_shows_the_reminders_they_were_sent(client, lib, db):
    member(db, email="priya@example.org")
    make_loan(client, lib, db, "R-SHOW", day(0))
    scan(db)
    queue.dispatch(db, build_providers(NotificationConfig()), NotificationConfig(), now=NOON)
    shown = client.get("/api/circulation/members/M0001", headers=lib).json()["reminders"]
    assert shown[0]["kind"] == "due_today" and shown[0]["channel"] == "email" and shown[0]["status"] == "sent"


def test_running_the_scan_by_api_needs_the_admin_permission(client, lib, admin):
    assert client.post("/api/notifications/reminders/run", headers=lib).status_code == 403
    r = client.post("/api/notifications/reminders/run", headers=admin)
    assert r.status_code == 200 and set(r.json()) >= {"queued", "due_soon", "due_today"}


# ---------- demo helper ----------
def test_demo_helper_moves_a_due_date_and_is_404_in_production(client, lib, db, settings):
    loan_id = make_loan(client, lib, db, "R-DEMO", day(14))
    r = client.post(f"/api/circulation/mock/loans/{loan_id}/due", json={"days_from_today": 1}, headers=lib)
    assert r.status_code == 200
    db.expire_all()
    assert reminders.library_day(db.get(Loan, loan_id).due_at) == (datetime.now(UTC) + timedelta(days=1)).date()
    assert (
        client.post(
            f"/api/circulation/mock/loans/{loan_id}/due", json={"days_from_today": 999}, headers=lib
        ).status_code
        == 422
    )
    assert (
        client.post("/api/circulation/mock/loans/99999/due", json={"days_from_today": 0}, headers=lib).status_code
        == 404
    )
    prod = dataclasses.replace(settings, environment="production", secret_key="x" * 40)
    with TestClient(create_app(prod, run_migrations=False)) as pc:
        pc_lib = login_card(pc, "STAFFLIB0001")
        assert (
            pc.post(
                f"/api/circulation/mock/loans/{loan_id}/due", json={"days_from_today": 0}, headers=pc_lib
            ).status_code
            == 404
        )


# ---------- the schema change ----------
def test_migration_0009_keeps_existing_members_and_notifications(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    migrate.upgrade(url, "0008")
    import sqlite3

    con = sqlite3.connect(tmp_path / "old.db")
    con.execute(
        "insert into members(member_no,name,status,block_reason,fines_cents,created_at)"
        " values ('M1','Old Member','active','',0,'2026-01-01')"
    )
    con.execute(
        "insert into notifications(created_at,channel,recipient,subject,body,status,attempts,last_error,"
        "next_attempt_at,provider_ref)"
        " values ('2026-01-01','email','a@example.org','s','b','sent',1,'','2026-01-01','r')"
    )
    con.commit()
    con.close()
    migrate.upgrade(url)
    con = sqlite3.connect(tmp_path / "old.db")
    assert con.execute("select email, phone, reminders_on from members").fetchone() == ("", "", 1)
    assert con.execute("select kind, loan_id, due_on from notifications").fetchone() == ("", None, "")
    con.close()
    migrate.downgrade(url, "0008")
    con = sqlite3.connect(tmp_path / "old.db")
    assert con.execute("select count(*) from members").fetchone()[0] == 1
    con.close()


# ---------- found by the bug hunt ----------
def bulk_loans(client, n, due):
    from aisys.models import BibRecord, Item

    with client.app.state.session_factory() as db:
        m = db.scalar(select(Member).where(Member.member_no == "M0001"))
        m.email, m.phone = "priya@example.org", "+15550100001"
        for i in range(n):
            bib = BibRecord(dedupe_key=f"bulk{i}", title=f"Bulk title {i}")
            db.add(bib)
            db.flush()
            item = Item(bib_id=bib.id, accession_no=f"B{i:05d}", status="on_loan")
            db.add(item)
            db.flush()
            db.add(Loan(member_id=m.id, item_id=item.id, due_at=due, out_by="x"))
        db.commit()


def test_one_pass_delivers_a_whole_days_backlog(client):
    bulk_loans(client, 150, day(0))  # 300 messages: three times what one dispatch call takes
    providers = build_providers(NotificationConfig())
    out = runner.run_once(client.app.state.session_factory, providers, NotificationConfig(), now=NOON)
    assert out["reminders"]["queued"] == 300
    assert out["delivery"]["sent"] == 300 and out["delivery"]["waiting"] == 0, "not 100 a minute"
    assert len(providers["email"].outbox) == 150 and len(providers["sms"].outbox) == 150


def test_two_dispatchers_never_send_the_same_message_twice(client):
    bulk_loans(client, 50, day(0))  # exactly 100 messages: one dispatch call takes them all
    factory = client.app.state.session_factory
    with factory() as db:
        scan(db)
    providers = build_providers(NotificationConfig())
    barrier = threading.Barrier(3)

    def go():
        barrier.wait()
        with factory() as s:
            queue.dispatch(s, providers, NotificationConfig(), now=NOON)

    threads = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    received = providers["email"].outbox + providers["sms"].outbox
    assert len(received) == 100, f"{len(received)} sent for 100 queued messages"
    assert len({m["subject"] + m["to"] for m in received}) == 100
    with factory() as s:
        assert {n.status for n in s.scalars(select(Notification))} == {"sent"}
        assert {n.attempts for n in s.scalars(select(Notification))} == {1}


def test_line_breaks_in_a_title_never_reach_a_subject(client, lib, db):
    member(db, email="priya@example.org", phone="+15550100001")
    item = make_item(client, lib, "R-NL", "Evil\r\nBcc: attacker@example.org")
    m = db.scalar(select(Member).where(Member.member_no == "M0001"))
    db.add(Loan(member_id=m.id, item_id=item["id"], due_at=day(0), out_by="x"))
    db.commit()
    scan(db)
    for n in queued(db):
        assert "\n" not in n.subject and "\r" not in n.subject
        assert "\r" not in n.body.split("Title:")[0]


@pytest.mark.parametrize(
    "channel, address",
    [("email", "a@example.org\n"), ("email", "a@example.org\nBcc: x@y.org"), ("sms", "+15550100001\n")],
)
def test_addresses_must_end_where_they_end(channel, address):
    from aisys.notifications.providers import valid_address

    assert not valid_address(channel, address)


def test_loop_interval_has_a_floor_and_zero_means_off(tmp_path):
    from aisys.config import load_settings

    def interval(text):
        path = tmp_path / "c.toml"
        path.write_text(f"[notifications]\n{text}\n")
        return load_settings(str(path)).notifications

    assert interval("auto_dispatch_seconds = 0.001").auto_dispatch_s == 1.0
    assert interval("auto_dispatch_seconds = 0").auto_dispatch_s == 0.0
    assert interval("auto_dispatch_seconds = -5").auto_dispatch_s == 0.0
    assert interval("auto_dispatch_seconds = 90").auto_dispatch_s == 90.0
    assert interval("remind_days_before = 0").remind_days_before == 1
    assert interval("remind_days_before = 99").remind_days_before == 30
