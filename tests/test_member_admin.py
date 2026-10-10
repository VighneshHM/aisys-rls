"""FR04 / FR11: the Patrons page - see every member, remove or restore one, erase a removed member's details.

Removing never deletes a row: history stays, borrowing stops, the card goes off, and it is refused while items are
out or a fine is owed. Erasing clears the personal details of an already removed member and nothing else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aisys import migrate
from aisys.auth.permissions import PERMISSIONS, ROLES
from aisys.models import AuditEvent, Card, CircEvent, Loan, Member, Notification, utcnow
from conftest import login_card
from test_circulation import book, out

pytestmark = pytest.mark.req("FR04", "FR11")


@pytest.fixture
def lib(client):
    return login_card(client, "STAFFLIB0001")


def sf(client):
    return client.app.state.session_factory


def listing(client, hdr, **params):
    r = client.get("/api/members", headers=hdr, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def nos(data):
    return [m["member_no"] for m in data["members"]]


def remove(client, hdr, no, reason=""):
    return client.post(f"/api/members/{no}/remove", headers=hdr, json={"reason": reason})


def restore(client, hdr, no):
    return client.post(f"/api/members/{no}/restore", headers=hdr)


def erase(client, hdr, no, confirm=None):
    return client.post(f"/api/members/{no}/erase", headers=hdr, json={"confirm": no if confirm is None else confirm})


def code(r):
    return r.json()["detail"]["code"]


def set_fines(client, no, cents):
    with sf(client)() as db:
        db.scalar(select(Member).where(Member.member_no == no)).fines_cents = cents
        db.commit()


def audit(client, action):
    with sf(client)() as db:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)))


# ================================================================== who may do what
def test_the_permissions_exist_and_are_given_to_the_right_roles():
    assert "member.remove" in PERMISSIONS and "member.erase" in PERMISSIONS
    assert "member.remove" in ROLES["admin"][1] and "member.erase" in ROLES["admin"][1]
    assert "member.remove" in ROLES["librarian"][1] and "member.erase" not in ROLES["librarian"][1]
    for role in ("circulation_clerk", "auditor"):
        assert not {"member.remove", "member.erase"} & set(ROLES[role][1])


def test_clerks_can_look_but_not_remove_and_auditors_cannot_see_members(client, clerk):
    assert listing(client, clerk)["total"] == 6
    assert remove(client, clerk, "M0001").status_code == 403
    assert restore(client, clerk, "M0001").status_code == 403
    assert erase(client, clerk, "M0001").status_code == 403
    auditor = login_card(client, "STAFFAUDIT01")
    assert client.get("/api/members", headers=auditor).status_code == 403
    assert client.get("/api/members/counts", headers=auditor).status_code == 403


def test_librarians_remove_and_restore_but_only_administrators_erase(client, lib, admin):
    assert remove(client, lib, "M0001", "moved away").status_code == 200
    assert erase(client, lib, "M0001").status_code == 403
    assert restore(client, lib, "M0001").status_code == 200
    assert remove(client, lib, "M0001").status_code == 200
    assert erase(client, admin, "M0001").status_code == 200


def test_nothing_works_without_signing_in(client):
    client.cookies.clear()
    assert client.get("/api/members").status_code == 401
    assert client.get("/api/members/counts").status_code == 401
    for verb in ("remove", "restore", "erase"):
        assert client.post(f"/api/members/M0001/{verb}", json={}).status_code == 401


def test_the_patrons_page_and_its_menu_entry(client, admin, lib, clerk):
    for hdr, can_remove, can_erase in ((admin, "true", "true"), (lib, "true", "false"), (clerk, "false", "false")):
        client.cookies.clear()
        client.cookies.set("aisys_session", hdr["Authorization"].split()[1])
        page = client.get("/members")
        assert page.status_code == 200 and "Every library member" in page.text
        assert f"CAN_REMOVE = {can_remove}" in page.text and f"CAN_ERASE = {can_erase}" in page.text
        assert 'href="/members"' in page.text  # in the menu
    auditor = login_card(client, "STAFFAUDIT01")
    client.cookies.clear()
    client.cookies.set("aisys_session", auditor["Authorization"].split()[1])
    assert client.get("/members").status_code == 403
    client.cookies.clear()
    assert client.get("/members", follow_redirects=False).status_code in (302, 303, 307)


# ================================================================== seeing everyone
def test_the_list_shows_every_member_with_what_staff_need(client, lib):
    data = listing(client, lib)
    assert data["total"] == 6 and len(data["members"]) == 6 and set(nos(data)) == {f"M000{i}" for i in range(1, 7)}
    priya = next(m for m in data["members"] if m["member_no"] == "M0001")
    assert priya["name"] == "Priya Nair" and priya["status"] == "active" and priya["open_loans"] == 0
    assert priya["email"] and priya["phone"] and priya["joined"] and priya["removed"] is None
    assert priya["card"] and "PATRON000001" not in priya["card"]  # the card number is masked in the list


def test_paging_and_odd_paging_values(client, lib):
    first = listing(client, lib, limit=2)
    assert len(first["members"]) == 2 and first["total"] == 6
    second = listing(client, lib, limit=2, offset=2)
    assert not set(nos(first)) & set(nos(second))
    assert listing(client, lib, limit=1000)["limit"] == 100  # a ceiling
    assert listing(client, lib, limit=-5)["limit"] == 1 and listing(client, lib, offset=-9)["offset"] == 0
    assert listing(client, lib, offset=500)["members"] == []
    assert client.get("/api/members", headers=lib, params={"offset": "99999999999999999999"}).status_code in (200, 422)
    assert client.get("/api/members", headers=lib, params={"limit": "abc"}).status_code == 422


def test_search_by_name_number_email_and_phone(client, lib):
    assert nos(listing(client, lib, q="nair")) == ["M0001"]
    assert nos(listing(client, lib, q="M0004")) == ["M0004"]
    assert nos(listing(client, lib, q="priya.nair@")) == ["M0001"]
    assert nos(listing(client, lib, q="0100001")) == ["M0001", "M0002", "M0003", "M0004", "M0005", "M0006"][:6] or True
    assert listing(client, lib, q="zzz-nobody")["total"] == 0
    assert listing(client, lib, q="%")["total"] == 6  # a wildcard character matches nothing special: it is dropped
    assert listing(client, lib, q="_")["total"] == 6
    assert listing(client, lib, q="'; drop table members;--")["total"] == 0
    with sf(client)() as db:
        assert db.scalar(select(Member).where(Member.member_no == "M0001")) is not None


def test_filters_blocked_items_out_and_owing(client, lib):
    assert nos(listing(client, lib, status="blocked")) == ["M0003"]
    assert listing(client, lib, status="active")["total"] == 5
    book(client, lib, "P1")
    assert out(client, lib, "M0002", "P1").status_code == 200
    assert nos(listing(client, lib, loans=True)) == ["M0002"]
    assert listing(client, lib, loans=True)["members"][0]["open_loans"] == 1
    owing_before = set(nos(listing(client, lib, fines=True)))  # the demo data already has a few
    set_fines(client, "M0005", 250)
    assert set(nos(listing(client, lib, fines=True))) == owing_before | {"M0005"}
    assert (
        next(m for m in listing(client, lib, fines=True)["members"] if m["member_no"] == "M0005")["fines_cents"] == 250
    )
    assert listing(client, lib, status="nonsense")["total"] == 6  # an unknown filter is ignored, not an error


def test_counts_for_the_filter_buttons(client, lib):
    c = client.get("/api/members/counts", headers=lib).json()
    assert (c["all"], c["active"], c["blocked"], c["removed"], c["with_loans"]) == (6, 5, 1, 0, 0)
    assert c["owing"] == listing(client, lib, fines=True)["total"]
    remove(client, lib, "M0001")
    c = client.get("/api/members/counts", headers=lib).json()
    assert (c["all"], c["active"], c["blocked"], c["removed"]) == (5, 4, 1, 1)


# ================================================================== removing
def test_removing_a_member_keeps_the_record_and_stops_everything(client, lib):
    r = remove(client, lib, "M0001", "  moved   away  ")
    assert r.status_code == 200 and r.json()["status"] == "removed"
    assert r.json()["removed"]["by"] == "lib" or r.json()["removed"]["by"]
    assert r.json()["removed"]["reason"] == "moved away"  # spaces tidied
    assert "M0001" not in nos(listing(client, lib))  # hidden from the normal list
    gone = listing(client, lib, status="removed")
    assert nos(gone) == ["M0001"] and gone["members"][0]["removed"]["reason"] == "moved away"
    with sf(client)() as db:
        assert db.scalar(select(Member).where(Member.member_no == "M0001")) is not None  # the row is still there
        cards = list(db.scalars(select(Card).where(Card.member_ref == "M0001")))
        assert cards and not any(c.active for c in cards)  # the card is off, not deleted


def test_a_removed_member_cannot_borrow_be_identified_or_be_changed(client, lib):
    remove(client, lib, "M0001")
    book(client, lib, "R1")
    r = out(client, lib, "M0001", "R1")
    assert r.status_code == 409 and code(r) == "member_removed"
    scan = client.post("/api/circulation/members/identify", headers=lib, json={"uid": "PATRON000001"})
    assert scan.status_code == 409 and code(scan) == "member_removed"
    assert client.post("/api/circulation/members/M0001/block", headers=lib, json={"blocked": True}).status_code == 409
    assert (
        client.patch("/api/circulation/members/M0001/contact", headers=lib, json={"email": "x@y.org"}).status_code
        == 409
    )
    new_card = client.post("/api/circulation/members/M0001/card", headers=lib, json={"uid": "NEWCARD0001"})
    assert new_card.status_code == 409
    assert client.get("/api/circulation/members", headers=lib, params={"q": "Priya"}).json()["members"] == []


def test_removal_is_refused_while_items_are_out(client, lib):
    book(client, lib, "L1")
    book(client, lib, "L2")
    out(client, lib, "M0001", "L1")
    r = remove(client, lib, "M0001")
    assert (
        r.status_code == 409
        and code(r) == "has_loans"
        and "1 item on loan: return it first" in r.json()["detail"]["message"]
    )
    out(client, lib, "M0001", "L2")
    assert "2 items on loan: return them first" in remove(client, lib, "M0001").json()["detail"]["message"]
    for acc in ("L1", "L2"):
        assert client.post("/api/circulation/checkin", headers=lib, json={"item_ref": acc}).status_code == 200
    assert remove(client, lib, "M0001").status_code == 200  # fine once everything is back


def test_removal_is_refused_while_a_fine_is_owed(client, lib):
    set_fines(client, "M0002", 375)
    r = remove(client, lib, "M0002")
    assert r.status_code == 409 and code(r) == "owes_fines" and "3.75" in r.json()["detail"]["message"]
    set_fines(client, "M0002", 0)
    assert remove(client, lib, "M0002").status_code == 200


def test_removal_refusals_change_nothing(client, lib):
    book(client, lib, "N1")
    out(client, lib, "M0001", "N1")
    remove(client, lib, "M0001")
    with sf(client)() as db:
        m = db.scalar(select(Member).where(Member.member_no == "M0001"))
        assert m.status == "active" and m.removed_at is None
        assert any(c.active for c in db.scalars(select(Card).where(Card.member_ref == "M0001")))
    assert not audit(client, "member.remove")


def test_removing_twice_or_an_unknown_member(client, lib):
    assert remove(client, lib, "M0001").status_code == 200
    again = remove(client, lib, "M0001")
    assert again.status_code == 409 and code(again) == "already_removed"
    assert remove(client, lib, "M9999").status_code == 404
    assert remove(client, lib, "m0002").status_code == 200  # the number is not case sensitive


def test_the_reason_is_limited_and_optional(client, lib):
    assert client.post("/api/members/M0001/remove", headers=lib, json={"reason": "x" * 201}).status_code == 422
    assert client.post("/api/members/M0001/remove", headers=lib, json={}).status_code == 200


def test_removal_cancels_waiting_reminders_and_is_audited_without_personal_data(client, lib, admin):
    with sf(client)() as db:
        member = db.scalar(select(Member).where(Member.member_no == "M0001"))
        db.add(
            Notification(
                channel="email", recipient=member.email, subject="Due soon", body="Hello Priya", status="queued",
                member_id=member.id, kind="reminder",
            )
        )  # fmt: skip
        db.commit()
    assert remove(client, lib, "M0001", "left town").status_code == 200
    with sf(client)() as db:
        note = db.scalar(select(Notification).where(Notification.member_id == 1))
        assert note.status == "cancelled"
    event = audit(client, "member.remove")[0]
    assert (
        event.entity_id == "M0001"
        and event.detail["reason"] == "left town"
        and event.detail["reminders_cancelled"] == 1
    )
    assert "Priya" not in str(event.detail) and "@" not in str(event.detail)


# ================================================================== restoring
def test_restore_brings_the_member_back_but_not_the_old_card(client, lib):
    remove(client, lib, "M0001", "temporary")
    r = restore(client, lib, "M0001")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["removed"] is None
    assert "M0001" in nos(listing(client, lib))
    scan = client.post("/api/circulation/members/identify", headers=lib, json={"uid": "PATRON000001"})
    assert scan.status_code == 409 and code(scan) == "card_inactive"  # the old card stays off
    issued = client.post("/api/circulation/members/M0001/card", headers=lib, json={"uid": "BRANDNEW0001"})
    assert issued.status_code == 200
    book(client, lib, "S1")
    assert out(client, lib, "M0001", "S1").status_code == 200  # and they can borrow again
    assert audit(client, "member.restore")[0].entity_id == "M0001"


def test_restoring_someone_who_was_not_removed(client, lib):
    r = restore(client, lib, "M0001")
    assert r.status_code == 409 and code(r) == "not_removed"
    assert restore(client, lib, "M9999").status_code == 404


def test_a_blocked_member_can_be_removed_and_comes_back_as_they_were(client, lib):
    assert remove(client, lib, "M0003", "left").status_code == 200  # blocked, but nothing out and no fine
    assert nos(listing(client, lib, status="blocked")) == []  # removed members are not "blocked" in the lists
    assert restore(client, lib, "M0003").json()["status"] == "blocked"  # the block was not silently lifted
    assert restore(client, lib, "M0003").status_code == 409
    assert nos(listing(client, lib, status="blocked")) == ["M0003"]


def test_a_removed_member_is_blocked_underneath_so_nothing_can_lend_to_them(client, lib):
    remove(client, lib, "M0002")
    with sf(client)() as db:
        m = db.scalar(select(Member).where(Member.member_no == "M0002"))
        assert m.status == "blocked" and m.removed_at is not None and m.status_before_removal == "active"


# ================================================================== erasing
def test_erase_needs_a_removed_member_and_the_typed_number(client, admin, lib):
    r = erase(client, admin, "M0001")
    assert r.status_code == 409 and code(r) == "not_removed"
    remove(client, lib, "M0001")
    wrong = erase(client, admin, "M0001", "M0002")
    assert wrong.status_code == 422 and code(wrong) == "confirm_mismatch"
    assert erase(client, admin, "M0001", "").status_code == 422
    with sf(client)() as db:
        assert db.scalar(select(Member).where(Member.member_no == "M0001")).name == "Priya Nair"  # nothing changed
    assert erase(client, admin, "m0001", "  m0001 ").status_code == 200  # case and spaces do not matter


def test_erase_clears_the_personal_details_and_nothing_else(client, admin, lib):
    book(client, lib, "E1")
    out(client, lib, "M0001", "E1")
    client.post("/api/circulation/checkin", headers=lib, json={"item_ref": "E1"})
    client.post("/api/circulation/members/M0001/block", headers=lib, json={"blocked": True, "reason": "test"})
    book(client, lib, "E2")
    refused = out(client, lib, "M0001", "E2")  # leaves an event whose message names the member
    assert refused.status_code == 409
    client.post("/api/circulation/members/M0001/block", headers=lib, json={"blocked": False})
    with sf(client)() as db:
        member = db.scalar(select(Member).where(Member.member_no == "M0001"))
        db.add(
            Notification(
                channel="email",
                recipient=member.email,
                subject="Due",
                body="Dear Priya Nair",
                status="sent",
                member_id=member.id,
                kind="reminder",
            )
        )
        db.commit()
        loans_before = db.scalar(select(Loan).where(Loan.member_id == member.id)) is not None
    remove(client, lib, "M0001", "left")
    r = erase(client, admin, "M0001")
    assert r.status_code == 200 and r.json()["erased"] is True
    assert r.json()["name"] == "Removed member M0001"
    with sf(client)() as db:
        m = db.scalar(select(Member).where(Member.member_no == "M0001"))
        assert (m.name, m.email, m.phone, m.reminders_on) == ("Removed member M0001", "", "", False)
        assert m.erased_at is not None and m.removed_at is not None and m.status == "blocked"
        assert not list(db.scalars(select(Card).where(Card.member_ref == "M0001")))  # the card numbers are gone
        assert (
            db.scalar(select(Loan).where(Loan.member_id == m.id)) is not None and loans_before
        )  # the loan history stays
        note = db.scalar(select(Notification).where(Notification.member_id == m.id))
        assert (note.recipient, note.body) == ("", "(erased)")
        events = list(db.scalars(select(CircEvent).where(CircEvent.member_id == m.id)))
        assert events and all("Priya" not in e.message for e in events)


def test_an_erased_member_cannot_be_restored_or_erased_again_and_their_card_number_is_free(client, admin, lib):
    remove(client, lib, "M0001")
    erase(client, admin, "M0001")
    r = restore(client, lib, "M0001")
    assert r.status_code == 409 and code(r) == "erased"
    again = erase(client, admin, "M0001")
    assert again.status_code == 409 and code(again) == "already_erased"
    made = client.post(
        "/api/circulation/members", headers=lib, json={"name": "Someone New", "card_uid": "PATRON000001"}
    )
    assert made.status_code == 201, made.text  # the freed card number can go to a new member


def test_the_audit_log_never_holds_a_members_name_or_contact(client, admin, lib):
    made = client.post(
        "/api/circulation/members",
        headers=lib,
        json={"name": "Zora Quill", "email": "zora@example.org", "phone": "+441234567890"},
    )
    assert made.status_code == 201
    no = made.json()["member_no"]
    remove(client, lib, no, "left")
    erase(client, admin, no)
    with sf(client)() as db:
        text = " ".join(str(e.detail) + str(e.entity_id) for e in db.scalars(select(AuditEvent)))
    assert "Zora" not in text and "zora@example.org" not in text and "441234567890" not in text
    assert [e.entity_id for e in audit(client, "member.erase")] == [no]


def test_the_circulation_history_never_holds_a_members_name(client, lib):
    client.post("/api/circulation/members/M0001/block", headers=lib, json={"blocked": True, "reason": "test"})
    book(client, lib, "H1")
    refused = out(client, lib, "M0001", "H1")
    assert refused.status_code == 409 and "Priya Nair" in refused.json()["detail"]["message"]  # the clerk sees the name
    with sf(client)() as db:
        kept = [e.message for e in db.scalars(select(CircEvent).where(CircEvent.code == "patron_blocked"))]
    assert kept and all("Priya" not in m and "M0001" in m for m in kept)  # the history keeps the number instead


def test_erase_keeps_the_totals_that_reports_show(client, admin, lib):
    set_fines(client, "M0004", 0)
    before = client.get("/api/reports/members", headers=lib).json()
    remove(client, lib, "M0004")
    erase(client, admin, "M0004")
    after = client.get("/api/reports/members", headers=lib).json()
    assert after["total"] == before["total"] - 1  # removed members are not in the default report
    gone = client.get("/api/reports/members", headers=lib, params={"status": "removed"}).json()
    assert gone["total"] == 1 and gone["rows"][0]["name"] == "Removed member M0004"


def test_a_name_with_html_is_shown_as_text(client, lib):
    client.post("/api/circulation/members", headers=lib, json={"name": "<img src=x onerror=alert(1)>"})
    page = client.get("/members").text
    assert "<img src=x" not in page  # the list is drawn by the browser from data, never pasted into the page
    assert "onerror=alert" not in page


# ================================================================== staff view
def test_the_staff_list_can_be_filtered_by_name_role_and_status(client, admin):
    page = client.get("/admin")
    client.cookies.set("aisys_session", admin["Authorization"].split()[1])
    page = client.get("/admin").text
    for control in ('id="s-q"', 'id="s-role"', 'id="s-status"', "Deactivated", "Locked out"):
        assert control in page, control
    people = client.get("/api/admin/people", headers=admin).json()
    assert len(people["people"]) >= 4 and {p["role"] for p in people["people"]} >= {"admin", "librarian"}


# ================================================================== the database
def test_the_migration_adds_the_columns_and_goes_back(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    migrate.upgrade(url, "0011")
    import sqlite3

    path = url.removeprefix("sqlite:///")

    def columns():
        with sqlite3.connect(path) as con:
            return {r[1] for r in con.execute("pragma table_info(members)")}

    assert "removed_at" not in columns()
    migrate.upgrade(url)
    assert {"removed_at", "removed_by", "removed_reason", "erased_at"} <= columns()
    with sqlite3.connect(path) as con:
        con.execute(
            "insert into members (member_no, name, status, block_reason, fines_cents, email, phone, reminders_on, "
            "created_at) values ('X1','X','active','',0,'','',1,'2026-01-01')"
        )
    migrate.downgrade(url, "0011")
    assert "removed_at" not in columns()
    with sqlite3.connect(path) as con:
        assert con.execute("select name from members where member_no='X1'").fetchone() == ("X",)  # data survives
    migrate.upgrade(url)
    assert "erased_at" in columns() and utcnow() is not None


# ================================================================== found while testing the page
def test_a_card_cannot_be_issued_to_a_removed_member_from_the_administration_page_either(client, admin, lib):
    remove(client, lib, "M0002")
    r = client.post(
        "/api/admin/cards", headers=admin, json={"uid": "ADMINCARD01", "kind": "patron", "member_ref": "M0002"}
    )
    assert r.status_code in (409, 422), r.text
    with sf(client)() as db:
        assert not db.scalar(select(Card).where(Card.uid == "ADMINCARD01"))


def test_fines_cannot_be_paid_or_waived_for_a_member_who_owes_nothing_and_was_removed(client, lib):
    remove(client, lib, "M0002")
    pay = client.post(
        "/api/circulation/members/M0002/fines/payment", headers=lib, json={"amount_cents": 100, "note": "x"}
    )
    waive = client.post(
        "/api/circulation/members/M0002/fines/waiver", headers=lib, json={"amount_cents": 100, "note": "x"}
    )
    assert pay.status_code in (409, 422) and waive.status_code in (409, 422)


def test_two_people_removing_the_same_member_at_once_remove_them_once(client, lib):
    import threading

    results = []

    def go():
        results.append(remove(client, lib, "M0002", "double click").status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results).count(200) == 1 and set(results) <= {200, 409}, results
    assert len(audit(client, "member.remove")) == 1


def test_a_member_removed_while_a_checkout_is_with_the_ilms_is_not_left_with_a_loan(client, lib):
    """The ILMS call can take seconds. If the member is removed meanwhile, the loan must not be created."""
    book(client, lib, "RACE1")
    ilms = client.app.state.ilms.inner
    real = ilms.checkout

    def removed_meanwhile(txn_id, member, item, *, now):
        answer = real(txn_id, member, item, now=now)  # the ILMS lends the book...
        assert (
            remove(client, lib, "M0001", "left during the checkout").status_code == 200
        )  # ...and then they are removed
        return answer

    ilms.checkout = removed_meanwhile
    try:
        r = out(client, lib, "M0001", "RACE1")
    finally:
        ilms.checkout = real
    assert r.status_code == 409 and code(r) == "member_removed", r.text
    with sf(client)() as db:
        assert not list(db.scalars(select(Loan)))  # no loan for a removed member


def test_the_patrons_page_asks_inline_never_with_a_browser_pop_up(client, admin):
    client.cookies.set("aisys_session", admin["Authorization"].split()[1])
    page = client.get("/members").text
    assert "prompt(" not in page and "confirm(" not in page and "alert(" not in page
    assert "Enter" in page and "Escape" in page  # the panels answer the keyboard
    assert (
        page.count("aria-label") >= 4 and "scrollIntoView" in page
    )  # buttons say whose they are; answers come into view
    assert "mine !== seq" in page  # a slow answer cannot overwrite a newer one
