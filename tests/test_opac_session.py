"""NFR02 / FR02: the public catalogue is for visitors, so opening it must end any staff session in that browser.

A person opens the public catalogue from the staff workspace and leaves the screen for patrons. Nobody must be able to
step from there back into the workspace (by "Staff sign-in", the Back button or typing an address) without signing in
again.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aisys.models import AuditEvent, AuthSession
from conftest import login_card

pytestmark = pytest.mark.req("NFR02", "FR02")


def sign_in_browser(client, uid="STAFFLIB0001") -> str:
    hdr = login_card(client, uid)
    token = hdr["Authorization"].split()[1]
    client.cookies.clear()
    client.cookies.set("aisys_session", token)
    return token


def sessions(client):
    with client.app.state.session_factory() as db:
        return list(db.scalars(select(AuthSession).order_by(AuthSession.id)))


def test_opening_the_public_catalogue_ends_the_staff_session(client):
    token = sign_in_browser(client)
    assert client.get("/dashboard", follow_redirects=False).status_code == 200  # signed in
    r = client.get("/opac")
    assert r.status_code == 200 and "Find a book" in r.text
    assert "aisys_session" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers["set-cookie"]
    assert all(s.revoked for s in sessions(client))  # the server forgot it, not only the browser
    client.cookies.set("aisys_session", token)  # even someone who kept the old cookie gets nowhere
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/dashboard", follow_redirects=False).status_code in (302, 303, 307)


def test_going_back_to_staff_sign_in_asks_for_a_sign_in(client):
    sign_in_browser(client)
    client.get("/opac")
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200 and "Staff sign-in" in page.text  # the form, not a redirect into the workspace
    for path in ("/dashboard", "/circulation", "/admin", "/settings", "/audit"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 307), path


def test_the_sign_out_is_recorded_with_its_reason(client, admin):
    sign_in_browser(client, "STAFFADMIN01")
    client.get("/opac")
    with client.app.state.session_factory() as db:
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.logout")))
    assert len(events) == 1 and events[0].detail == {"reason": "public_catalogue"} and events[0].actor == "admin"


def test_a_visitor_with_no_session_just_sees_the_catalogue(client):
    client.cookies.clear()
    r = client.get("/opac")
    assert r.status_code == 200 and "set-cookie" not in r.headers
    with client.app.state.session_factory() as db:
        assert not list(db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.logout")))


def test_other_staff_sessions_are_not_touched(client):
    other = login_card(client, "STAFFCLERK01")  # a different person's session (another computer)
    sign_in_browser(client, "STAFFLIB0001")  # this browser
    client.get("/opac")
    assert client.get("/api/auth/me", headers=other).status_code == 200
    assert client.get("/api/auth/me").status_code == 401  # this browser's cookie was ended


def test_a_switched_off_public_catalogue_does_not_sign_anyone_out(client, admin):
    client.put("/api/admin/settings", headers=admin, json={"values": {"opac.enabled": False}})
    token = sign_in_browser(client)
    assert client.get("/opac").status_code == 404
    assert client.get("/api/auth/me").status_code == 200  # still signed in: there was no public page to expose it
    assert token


def test_the_landing_page_offers_the_public_catalogue(client):
    client.cookies.clear()
    page = client.get("/")
    assert 'href="/opac"' in page.text and "public catalogue" in page.text.lower()


def test_the_staff_menu_says_the_public_catalogue_signs_you_out(client):
    sign_in_browser(client)
    page = client.get("/circulation").text
    assert 'href="/opac"' in page and "Signs out" in page and "signs you out" in page


def test_pages_are_never_kept_for_the_back_button(client):
    sign_in_browser(client)
    for path in ("/", "/dashboard", "/opac"):
        r = client.get(path, follow_redirects=False)
        if r.headers.get("content-type", "").startswith("text/html"):
            assert r.headers.get("cache-control") == "no-store", path
    assert "no-store" not in client.get("/static/app.css").headers.get("cache-control", "")


def test_the_staff_menu_can_scroll_when_there_are_more_entries_than_the_screen_holds(client):
    css = client.get("/static/app.css").text
    nav = next(line for line in css.splitlines() if line.startswith(".nav {"))
    assert "overflow-y: auto" in nav and "min-height: 0" in nav
    side = next(line for line in css.splitlines() if line.startswith(".side {"))
    assert "height: 100vh" in side  # the menu scrolls inside a fixed-height sidebar rather than being cut off


def test_the_staff_menu_remembers_where_it_was_scrolled_between_pages(client):
    sign_in_browser(client)
    page = client.get("/circulation").text
    assert "aisys.nav.scroll" in page and "sessionStorage" in page and "scrollIntoView" in page
    assert "a.active" in page  # and the page you are on is never left out of sight
    # it runs once the whole sidebar exists (so the sizes are final), and before the page content
    assert page.index('class="ver"') < page.index("aisys.nav.scroll") < page.index('<main class="content">')
