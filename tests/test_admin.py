"""FR11: the Audit log and Administration pages and their API."""

from __future__ import annotations

import csv
import io

import pytest

from conftest import login_card

pytestmark = pytest.mark.req("FR11")


@pytest.fixture
def aud(client):
    return login_card(client, "STAFFAUDIT01")


def people(client, headers):
    r = client.get("/api/admin/people", headers=headers)
    assert r.status_code == 200, r.text
    return {p["username"]: p for p in r.json()["people"]}


# ---------- audit log ----------
def test_audit_events_are_paged_and_filtered(client, admin, aud):
    for _ in range(3):  # three denied attempts leave three audit lines
        client.post("/api/auth/card-login", json={"uid": "NOSUCHCARD1"})
    d = client.get("/api/audit/events?limit=2", headers=aud).json()
    assert len(d["events"]) == 2 and d["total"] >= 3 and d["limit"] == 2 and "admin" in d["actors"]
    page2 = client.get("/api/audit/events?limit=2&offset=2", headers=aud).json()["events"]
    assert page2 and page2[0]["id"] < d["events"][-1]["id"], "newest first, the next page is older"
    denied = client.get("/api/audit/events?outcome=denied&action=auth.login", headers=aud).json()
    assert denied["total"] >= 3 and all(e["outcome"] == "denied" for e in denied["events"])
    assert client.get("/api/audit/events?actor=admin", headers=aud).json()["total"] >= 1
    assert client.get("/api/audit/events?q=nothing-matches-this", headers=aud).json()["total"] == 0


def test_audit_search_text_is_not_a_wildcard(client, aud):
    everything = client.get("/api/audit/events", headers=aud).json()["total"]
    assert everything > 0
    for sneaky in ("%", "_", "%%", "\\"):
        assert client.get("/api/audit/events", params={"action": sneaky}, headers=aud).json()["total"] == 0
        assert client.get("/api/audit/events", params={"q": sneaky}, headers=aud).json()["total"] == 0


def test_audit_page_size_is_capped_and_bad_values_are_safe(client, aud):
    assert client.get("/api/audit/events?limit=100000", headers=aud).json()["limit"] == 100
    assert client.get("/api/audit/events?limit=-5&offset=-9", headers=aud).json()["limit"] == 1
    assert client.get("/api/audit/events?offset=999999", headers=aud).json()["events"] == []


def test_audit_requires_the_permission(client, clerk):
    assert client.get("/api/audit/events", headers=clerk).status_code == 403
    assert client.get("/api/audit/export.csv", headers=clerk).status_code == 403
    client.cookies.clear()
    assert client.get("/api/audit/events").status_code == 401


def test_audit_export_is_csv_and_neutralises_formulas(client, aud):
    client.post("/api/auth/card-login", json={"uid": "=CMD|'/c calc'!A1"})  # hostile text lands in the log
    r = client.get("/api/audit/export.csv?outcome=denied", headers=aud)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:4] == ["id", "time", "actor", "action"] and len(rows) > 1
    for row in rows[1:]:
        assert all(not c.startswith(("=", "+", "-", "@")) for c in row), row


def test_pages_render_for_the_right_roles(client, admin, aud, clerk):
    for headers, path, status in (
        (aud, "/audit", 200),
        (admin, "/audit", 200),
        (admin, "/admin", 200),
        (aud, "/admin", 403),
        (clerk, "/audit", 403),
        (clerk, "/admin", 403),
    ):
        client.cookies.set("aisys_session", headers["Authorization"].split()[1])
        assert client.get(path).status_code == status, (path, status)
    client.cookies.clear()
    assert client.get("/audit", follow_redirects=False).status_code in (302, 303, 307)


# ---------- administration ----------
def test_people_lists_staff_with_masked_cards(client, admin):
    p = people(client, admin)
    assert set(p) >= {"admin", "librarian", "clerk", "auditor"}
    card = p["admin"]["cards"][0]
    assert "…" in card["uid_hint"] and "STAFFADMIN01" not in str(p), "full card numbers are never listed"
    assert p["admin"]["role"] == "admin" and p["admin"]["active"] and not p["admin"]["locked"]
    roles = {r["name"] for r in client.get("/api/admin/people", headers=admin).json()["roles"]}
    assert {"admin", "auditor", "librarian", "circulation_clerk"} <= roles
    assert "password_hash" not in str(client.get("/api/admin/people", headers=admin).json())


def test_only_admin_manages_people(client, clerk, aud):
    for h in (clerk, aud):
        assert client.get("/api/admin/people", headers=h).status_code == 403
        assert client.patch("/api/admin/users/1", json={"active": False}, headers=h).status_code == 403
        assert client.patch("/api/admin/cards/1", json={"active": False}, headers=h).status_code == 403


def test_deactivating_a_user_ends_their_session_and_blocks_their_card(client, admin):
    clerk_h = login_card(client, "STAFFCLERK01")
    cid = people(client, admin)["clerk"]["id"]
    r = client.patch(f"/api/admin/users/{cid}", json={"active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.get("/api/auth/me", headers=clerk_h).status_code == 401, "a live session ends at once"
    assert client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"}).status_code in (401, 403)
    client.patch(f"/api/admin/users/{cid}", json={"active": True}, headers=admin)
    assert client.post("/api/auth/card-login", json={"uid": "STAFFCLERK01"}).status_code == 200


def test_cannot_lock_yourself_out(client, admin):
    me = people(client, admin)["admin"]["id"]
    assert client.patch(f"/api/admin/users/{me}", json={"active": False}, headers=admin).status_code == 409
    assert client.patch(f"/api/admin/users/{me}", json={"role": "auditor"}, headers=admin).status_code == 409
    assert people(client, admin)["admin"]["active"] and people(client, admin)["admin"]["role"] == "admin"
    # another administrator may be added and removed freely
    r = client.post(
        "/api/admin/users", json={"username": "second.admin", "display_name": "Second", "role": "admin"}, headers=admin
    )
    second = r.json()["id"]
    assert client.patch(f"/api/admin/users/{second}", json={"active": False}, headers=admin).status_code == 200


def test_role_change_and_unknown_role(client, admin):
    cid = people(client, admin)["clerk"]["id"]
    assert (
        client.patch(f"/api/admin/users/{cid}", json={"role": "librarian"}, headers=admin).json()["role"] == "librarian"
    )
    assert client.patch(f"/api/admin/users/{cid}", json={"role": "wizard"}, headers=admin).status_code == 422
    assert client.patch("/api/admin/users/99999", json={"active": False}, headers=admin).status_code == 404


def test_unlocking_a_locked_account(client, admin):
    r = client.post(
        "/api/admin/users",
        json={"username": "pw.user", "display_name": "Pw User", "role": "librarian", "password": "a-long-password-1"},
        headers=admin,
    )
    uid = r.json()["id"]
    for _ in range(3):  # max_failed_logins is 3 in the test settings
        client.post("/api/auth/login", json={"username": "pw.user", "password": "wrong-password-1"})
    assert (
        client.post("/api/auth/login", json={"username": "pw.user", "password": "a-long-password-1"}).status_code != 200
    )
    locked = people(client, admin)["pw.user"]
    assert locked["locked"] is True and locked["locked_until"]
    assert client.patch(f"/api/admin/users/{uid}", json={"unlock": True}, headers=admin).json()["locked"] is False
    assert (
        client.post("/api/auth/login", json={"username": "pw.user", "password": "a-long-password-1"}).status_code == 200
    )


def test_card_deactivate_and_reactivate(client, admin):
    card = people(client, admin)["librarian"]["cards"][0]
    r = client.patch(f"/api/admin/cards/{card['id']}", json={"active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"}).status_code in (401, 403)
    client.patch(f"/api/admin/cards/{card['id']}", json={"active": True}, headers=admin)
    assert client.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"}).status_code == 200
    assert client.patch("/api/admin/cards/99999", json={"active": True}, headers=admin).status_code == 404
    assert client.patch(f"/api/admin/cards/{card['id']}", json={"active": "maybe"}, headers=admin).status_code == 422


def test_changes_are_audited_without_secrets(client, admin, aud):
    cid = people(client, admin)["clerk"]["id"]
    client.patch(f"/api/admin/users/{cid}", json={"active": False, "role": "librarian"}, headers=admin)
    card = people(client, admin)["librarian"]["cards"][0]
    client.patch(f"/api/admin/cards/{card['id']}", json={"active": False}, headers=admin)
    log = client.get("/api/audit/events?action=admin.", headers=aud).json()["events"]
    actions = [e["action"] for e in log]
    assert "admin.user.update" in actions and "admin.card.deactivate" in actions
    text = str(log)
    assert "STAFFLIB0001" not in text and "password" not in text.lower()
    upd = next(e for e in log if e["action"] == "admin.user.update")
    assert upd["actor"] == "admin" and upd["detail"]["active"] is False and upd["detail"]["role"]["to"] == "librarian"


def test_no_change_writes_no_audit_line(client, admin, aud):
    cid = people(client, admin)["clerk"]["id"]
    before = client.get("/api/audit/events?action=admin.user.update", headers=aud).json()["total"]
    client.patch(f"/api/admin/users/{cid}", json={"active": True}, headers=admin)  # already active
    client.patch(f"/api/admin/users/{cid}", json={}, headers=admin)
    assert client.get("/api/audit/events?action=admin.user.update", headers=aud).json()["total"] == before


# ---------- found by the bug hunt ----------
def test_enormous_numbers_are_a_clean_422_everywhere(client, admin, aud):
    huge = "99999999999999999999"
    for path in (
        f"/api/audit/events?offset={huge}",
        f"/api/catalogue/items?offset={huge}",
        f"/api/gate/events?offset={huge}",
        f"/api/rfid/tags?offset={huge}",
        f"/api/migrations/1/rows?offset={huge}",
    ):
        r = client.get(path, headers=aud if "audit" in path else admin)
        assert r.status_code in (404, 422), (path, r.status_code)
    assert client.patch(f"/api/admin/users/{huge}", json={"active": False}, headers=admin).status_code == 422
    assert client.patch(f"/api/admin/cards/{huge}", json={"active": False}, headers=admin).status_code == 422


def test_card_issuing_rules(client, admin):
    ghost = client.post(
        "/api/admin/cards", json={"uid": "PATRONGHOST1", "kind": "patron", "member_ref": "M99999"}, headers=admin
    )
    assert ghost.status_code == 422 and "M99999" in str(ghost.json())
    assert (
        client.post(
            "/api/admin/cards", json={"uid": "PATRONGOOD01", "kind": "patron", "member_ref": "M0001"}, headers=admin
        ).status_code
        == 201
    )
    cid = people(client, admin)["clerk"]["id"]
    client.patch(f"/api/admin/users/{cid}", json={"active": False}, headers=admin)
    off = client.post(
        "/api/admin/cards", json={"uid": "STAFFINACT01", "kind": "staff", "username": "clerk"}, headers=admin
    )
    assert off.status_code == 409 and "deactivated" in str(off.json())


# ---------- roles ----------
def roles(client, headers):
    r = client.get("/api/admin/roles", headers=headers)
    assert r.status_code == 200, r.text
    return {x["name"]: x for x in r.json()["roles"]}


def test_roles_list_the_built_in_ones_and_the_permission_catalogue(client, admin):
    d = client.get("/api/admin/roles", headers=admin).json()
    by_name = {r["name"]: r for r in d["roles"]}
    assert {"admin", "librarian", "circulation_clerk", "auditor"} <= set(by_name)
    assert all(by_name[n]["built_in"] for n in ("admin", "librarian", "circulation_clerk", "auditor"))
    assert by_name["circulation_clerk"]["holders"] >= 1
    assert "circulation.checkout" in {p["code"] for p in d["permissions"]}


def test_a_custom_role_is_created_given_to_someone_and_enforced(client, admin):
    r = client.post(
        "/api/admin/roles",
        json={
            "name": "Stock_Checker",
            "description": "  Shelf   reading only ",
            "permissions": ["catalogue.read", "inventory.run"],
        },
        headers=admin,
    )
    assert r.status_code == 201, r.text
    assert r.json() == {
        "name": "stock_checker",
        "description": "Shelf reading only",
        "built_in": False,
        "permissions": ["catalogue.read", "inventory.run"],
        "holders": 0,
    }
    cid = people(client, admin)["clerk"]["id"]
    assert client.patch(f"/api/admin/users/{cid}", json={"role": "stock_checker"}, headers=admin).status_code == 200
    clerk_h = login_card(client, "STAFFCLERK01")
    me = client.get("/api/auth/me", headers=clerk_h).json()
    assert set(me["permissions"]) == {"catalogue.read", "inventory.run"}
    assert client.get("/api/circulation/members?q=priya", headers=clerk_h).status_code == 403, "member.read is gone"
    # widening the role takes effect on the next request, without signing in again
    assert (
        client.patch(
            "/api/admin/roles/stock_checker",
            json={"permissions": ["catalogue.read", "inventory.run", "member.read"]},
            headers=admin,
        ).json()["holders"]
        == 1
    )
    assert client.get("/api/circulation/members?q=priya", headers=clerk_h).status_code == 200


def test_bad_roles_are_refused(client, admin):
    def create(**body):
        return client.post("/api/admin/roles", json={"permissions": ["catalogue.read"], **body}, headers=admin)

    assert create(name="librarian").status_code == 409, "a built-in name cannot be reused"
    assert create(name="x").status_code == 422
    assert create(name="9lives").status_code == 422
    assert create(name="has space").status_code == 422
    assert create(name="okname", permissions=["wizardry"]).status_code == 422
    assert create(name="okname", permissions=[]).status_code == 422
    assert create(name="okname").status_code == 201
    assert create(name="okname").status_code == 409


def test_built_in_roles_cannot_be_changed_or_removed(client, admin):
    for name in ("admin", "librarian", "circulation_clerk", "auditor"):
        assert client.patch(f"/api/admin/roles/{name}", json={"description": "x"}, headers=admin).status_code == 409
        assert client.delete(f"/api/admin/roles/{name}", headers=admin).status_code == 409
    assert client.patch("/api/admin/roles/nosuch", json={"description": "x"}, headers=admin).status_code == 404
    assert client.delete("/api/admin/roles/nosuch", headers=admin).status_code == 404


def test_a_role_in_use_cannot_be_removed_and_an_unused_one_can(client, admin):
    client.post("/api/admin/roles", json={"name": "night_desk", "permissions": ["catalogue.read"]}, headers=admin)
    cid = people(client, admin)["clerk"]["id"]
    client.patch(f"/api/admin/users/{cid}", json={"role": "night_desk"}, headers=admin)
    r = client.delete("/api/admin/roles/night_desk", headers=admin)
    assert r.status_code == 409 and "another" in r.text
    client.patch(f"/api/admin/users/{cid}", json={"role": "circulation_clerk"}, headers=admin)
    assert client.delete("/api/admin/roles/night_desk", headers=admin).status_code == 200
    assert "night_desk" not in roles(client, admin)


def test_an_administrator_cannot_take_admin_users_from_their_own_role(client, admin):
    client.post(
        "/api/admin/roles", json={"name": "deputy_admin", "permissions": ["admin.users", "audit.read"]}, headers=admin
    )
    r = client.post(
        "/api/admin/users", json={"username": "deputy", "display_name": "Deputy", "role": "deputy_admin"}, headers=admin
    )
    client.post("/api/admin/cards", json={"uid": "STAFFDEPUTY1", "kind": "staff", "username": "deputy"}, headers=admin)
    assert r.status_code == 201
    deputy = login_card(client, "STAFFDEPUTY1")
    cut = client.patch("/api/admin/roles/deputy_admin", json={"permissions": ["audit.read"]}, headers=deputy)
    assert cut.status_code == 409
    assert "admin.users" in roles(client, admin)["deputy_admin"]["permissions"]
    # another administrator may do it
    assert (
        client.patch("/api/admin/roles/deputy_admin", json={"permissions": ["audit.read"]}, headers=admin).status_code
        == 200
    )


@pytest.mark.req("NFR02")
def test_managing_staff_never_hands_out_more_than_you_hold(client, admin):
    """D-071: a role with only admin.users could make a full administrator and sign in as them."""
    client.post("/api/admin/roles", json={"name": "user_manager", "permissions": ["admin.users"]}, headers=admin)
    client.post(
        "/api/admin/users", json={"username": "um1", "display_name": "UM", "role": "user_manager"}, headers=admin
    )
    client.post("/api/admin/cards", json={"uid": "USERMGR001", "kind": "staff", "username": "um1"}, headers=admin)
    um = login_card(client, "USERMGR001")
    ids = {u: p["id"] for u, p in people(client, admin).items()}

    def refused(r):
        return r.status_code == 403 and "rights you do not hold" in r.text

    shadow = {"username": "shadow", "display_name": "S", "role": "admin"}
    assert refused(client.post("/api/admin/users", json=shadow, headers=um))
    assert refused(
        client.post("/api/admin/roles", json={"name": "superish", "permissions": ["admin.config"]}, headers=um)
    )
    grow = {"permissions": ["admin.users", "admin.config"]}
    assert refused(client.patch("/api/admin/roles/user_manager", json=grow, headers=um))
    assert refused(client.patch(f"/api/admin/users/{ids['clerk']}", json={"role": "admin"}, headers=um))
    assert refused(client.patch(f"/api/admin/users/{ids['admin']}", json={"active": False}, headers=um))
    assert refused(
        client.post("/api/admin/cards", json={"uid": "SHADOW0001", "kind": "staff", "username": "admin"}, headers=um)
    )
    admin_card = next(c for c in people(client, admin)["admin"]["cards"])
    assert refused(client.patch(f"/api/admin/cards/{admin_card['id']}", json={"active": False}, headers=um))
    # within its own rights it still works: a peer with the same role, and its own card
    peer = {"username": "um2", "display_name": "UM2", "role": "user_manager"}
    assert client.post("/api/admin/users", json=peer, headers=um).status_code == 201
    assert (
        client.post(
            "/api/admin/cards", json={"uid": "USERMGR002", "kind": "staff", "username": "um2"}, headers=um
        ).status_code
        == 201
    )
    denied = client.get("/api/audit/events?action=authz.denied", headers=admin).json()["events"]
    assert any(e["detail"].get("beyond_own_rights") for e in denied)


def test_role_changes_are_audited_and_only_for_administrators(client, admin, aud, clerk):
    for h in (aud, clerk):
        assert client.get("/api/admin/roles", headers=h).status_code == 403
        assert (
            client.post(
                "/api/admin/roles", json={"name": "abc", "permissions": ["catalogue.read"]}, headers=h
            ).status_code
            == 403
        )
    client.post("/api/admin/roles", json={"name": "temp_role", "permissions": ["catalogue.read"]}, headers=admin)
    client.patch("/api/admin/roles/temp_role", json={"permissions": ["catalogue.read", "reports.view"]}, headers=admin)
    client.delete("/api/admin/roles/temp_role", headers=admin)
    events = client.get("/api/audit/events?action=admin.role", headers=aud).json()["events"]
    assert [e["action"] for e in events] == ["admin.role.delete", "admin.role.update", "admin.role.create"]
    assert events[1]["detail"] == {"added": ["reports.view"], "removed": []}


def test_built_in_roles_are_restored_at_start(client, admin):
    from aisys.auth.permissions import sync_rbac
    from aisys.models import Role

    with client.app.state.session_factory() as db:
        db.query(Role).filter_by(name="auditor").one().permissions = []
        db.commit()
        sync_rbac(db)  # what every start does
    assert "audit.read" in roles(client, admin)["auditor"]["permissions"]
