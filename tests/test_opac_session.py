"""NFR02 / FR02: the public catalogue and staff sessions (decision DR-17).

The public catalogue is reached from the sign-in page, which shows only once nobody is signed in, so the normal way to
it ends the staff session first (this keeps D-052 closed); it is not in the staff menu. Merely opening the catalogue
changes nothing, so a link from another site, a second tab at the desk or an API client never signs anyone out
(D-055); if a staff session is still open there, the page warns and offers a sign-out (a POST to /handover). A public
terminal uses a kiosk host name ([opac] kiosk_hosts), where only the catalogue exists and no staff session can be used
or created.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aisys.app import create_app
from aisys.cli import seed_demo
from aisys.config import load_settings
from aisys.models import AuditEvent, AuthSession
from conftest import login_card

pytestmark = pytest.mark.req("NFR02", "FR02")
KIOSK = "opac.library.local"


def sign_in_browser(client, uid="STAFFLIB0001") -> str:
    hdr = login_card(client, uid)
    token = hdr["Authorization"].split()[1]
    client.cookies.clear()
    client.cookies.set("aisys_session", token)
    return token


def sessions(client):
    with client.app.state.session_factory() as db:
        return list(db.scalars(select(AuthSession).order_by(AuthSession.id)))


def logouts(client):
    with client.app.state.session_factory() as db:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.logout")))


# ---------------------------------------------------------------- opening the catalogue has no side effect (D-055)
def test_opening_the_public_catalogue_signs_nobody_out_but_warns_on_a_staff_browser(client):
    sign_in_browser(client)
    r = client.get("/opac")
    assert r.status_code == 200 and "Find a book" in r.text and "set-cookie" not in r.headers
    assert "still signed in" in r.text and 'action="/handover"' in r.text  # the way to hand the screen over
    assert client.get("/api/auth/me").status_code == 200
    assert not any(s.revoked for s in sessions(client)) and not logouts(client)


def test_a_link_from_another_site_does_not_sign_staff_out(client):
    sign_in_browser(client)
    r = client.get("/opac", headers={"referer": "https://elsewhere.example/news", "sec-fetch-site": "cross-site"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_an_api_client_that_reads_the_catalogue_keeps_its_token(client):
    hdr = login_card(client, "STAFFLIB0001")
    client.cookies.clear()
    assert client.get("/opac", headers=hdr).status_code == 200
    assert client.get("/api/auth/me", headers=hdr).status_code == 200


def test_a_visitor_with_no_session_sees_just_the_catalogue(client):
    client.cookies.clear()
    r = client.get("/opac")
    assert r.status_code == 200 and "set-cookie" not in r.headers and "still signed in" not in r.text


# ---------------------------------------------------------------- handing a screen over (D-052 stays fixed)
def test_handing_over_ends_the_session_records_why_and_opens_the_catalogue(client):
    token = sign_in_browser(client, "STAFFADMIN01")
    r = client.post("/handover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/opac"
    assert "aisys_session" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers["set-cookie"]
    assert all(s.revoked for s in sessions(client))  # the server forgot it, not only the browser
    [ev] = logouts(client)
    assert ev.detail == {"reason": "public_catalogue"} and ev.actor == "admin"
    client.cookies.set("aisys_session", token)  # even someone who kept the old cookie gets nowhere
    assert client.get("/api/auth/me").status_code == 401
    for path in ("/dashboard", "/circulation", "/admin", "/settings", "/audit"):
        assert client.get(path, follow_redirects=False).status_code in (302, 303, 307), path
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200 and "Staff sign-in" in page.text  # the form, not a way into the workspace


def test_a_hand_over_from_another_site_is_refused(client):
    sign_in_browser(client)
    r = client.post("/handover", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/api/auth/me").status_code == 200 and not logouts(client)


def test_a_hand_over_without_a_session_just_opens_the_catalogue(client):
    client.cookies.clear()
    r = client.post("/handover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/opac" and not logouts(client)


def test_handing_over_one_screen_leaves_other_sessions_alone(client):
    other = login_card(client, "STAFFCLERK01")  # a different person's session (another computer)
    sign_in_browser(client, "STAFFLIB0001")
    client.post("/handover")
    assert client.get("/api/auth/me", headers=other).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_with_the_catalogue_switched_off_a_hand_over_goes_to_the_home_page(client, admin):
    client.put("/api/admin/settings", headers=admin, json={"values": {"opac.enabled": False}})
    sign_in_browser(client)
    r = client.post("/handover", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/opac").status_code == 404


def test_the_public_catalogue_is_not_in_the_staff_menu(client):
    for uid in ("STAFFADMIN01", "STAFFLIB0001", "STAFFCLERK01", "STAFFAUDIT01"):
        sign_in_browser(client, uid)
        page = client.get("/dashboard" if uid != "STAFFCLERK01" else "/circulation")
        assert page.status_code == 200, uid
        assert 'href="/opac"' not in page.text and "/handover" not in page.text, uid
        assert "Public catalogue" not in page.text, uid


def test_the_way_to_the_catalogue_from_the_workspace_goes_through_signing_out(client):
    sign_in_browser(client)
    assert client.get("/", follow_redirects=False).status_code in (302, 303, 307), "signed in: no landing page"
    client.post("/api/auth/logout")
    client.cookies.clear()
    landing = client.get("/")
    assert landing.status_code == 200 and 'href="/opac"' in landing.text  # only now is the catalogue offered


def test_the_landing_page_offers_the_public_catalogue(client):
    client.cookies.clear()
    page = client.get("/")
    assert 'href="/opac"' in page.text and "public catalogue" in page.text.lower()


def test_the_public_page_has_only_one_way_out_the_home_page(client):
    client.cookies.clear()
    page = client.get("/opac").text
    assert "Return to home page" in page and 'href="/"' in page
    assert "Staff sign-in" not in page  # no staff-flavoured shortcut on the visitors' page


# ---------------------------------------------------------------- kiosk host for public terminals
@pytest.fixture
def kiosk(settings):
    s = replace(settings, opac_kiosk_hosts=(KIOSK,))
    app = create_app(s)
    seed_demo(s)
    with TestClient(app) as c:
        yield c


def on_kiosk(client, method, path, **kw):
    return client.request(method, path, headers={"host": KIOSK, **kw.pop("headers", {})}, follow_redirects=False, **kw)


def test_a_kiosk_host_serves_only_the_public_catalogue(kiosk):
    assert on_kiosk(kiosk, "GET", "/").headers["location"] == "/opac"
    assert on_kiosk(kiosk, "GET", "/opac").status_code == 200
    assert on_kiosk(kiosk, "GET", "/api/opac/search", params={"q": "anything"}).status_code == 200
    assert on_kiosk(kiosk, "GET", "/static/app.css").status_code == 200
    for method, path in (
        ("GET", "/dashboard"),
        ("GET", "/admin"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/card-login"),
        ("POST", "/api/auth/login"),
        ("POST", "/handover"),
        ("GET", "/docs"),
        ("GET", "/api/catalogue/items"),
    ):
        assert on_kiosk(kiosk, method, path).status_code == 404, path


def test_a_kiosk_host_never_sees_or_creates_a_staff_session(kiosk):
    token = sign_in_browser(kiosk, "STAFFADMIN01")  # a cookie that somehow reached the kiosk browser
    r = on_kiosk(kiosk, "GET", "/opac", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200 and "still signed in" not in r.text and "set-cookie" not in r.headers
    assert kiosk.get("/api/auth/me").status_code == 200, "the staff host is unaffected: nobody was signed out"


def test_the_staff_host_is_unchanged_when_kiosk_hosts_are_set(kiosk):
    assert kiosk.get("/", follow_redirects=False).status_code == 200  # the sign-in page, not the catalogue
    assert kiosk.post("/api/auth/card-login", json={"uid": "STAFFLIB0001"}).status_code == 200


def test_kiosk_host_names_are_checked(tmp_path):
    cfg = tmp_path / "k.toml"
    for bad in ('"http://opac.local"', '"opac.local:8000"', '["bad host"]', "42"):
        cfg.write_text(f"[opac]\nkiosk_hosts = {bad}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="kiosk_hosts"):
            load_settings(cfg)
    cfg.write_text('[opac]\nkiosk_hosts = ["OPAC.Library.local", "kiosk-2"]\n', encoding="utf-8")
    assert load_settings(cfg).opac_kiosk_hosts == ("opac.library.local", "kiosk-2")


# ---------------------------------------------------------------- browser memory and the staff menu
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
