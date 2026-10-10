"""NFR02 / NFR03: masked card numbers, safe headers, encrypted transport (real HTTPS), the environment doctor."""

from __future__ import annotations

import dataclasses
import datetime
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aisys import doctor
from aisys.app import create_app
from aisys.auth.security import card_hint
from aisys.cli import main as cli_main
from aisys.cli import serve_options
from conftest import login_card

pytestmark = pytest.mark.req("NFR02", "NFR03")


# ---------- card numbers are masked in the audit log ----------
def test_card_hint_shows_which_card_but_never_the_whole_number():
    assert card_hint("STAFFADMIN01") == "STA…N01"
    assert card_hint("ABC") == "ABC" and card_hint("") == ""
    assert "STAFFADMIN01" not in card_hint("STAFFADMIN01")


def test_the_audit_log_never_holds_a_whole_card_number(client, admin):
    client.post("/api/auth/card-login", json={"uid": "NOSUCHCARD99"})
    client.post("/api/auth/card-login", json={"uid": "PATRON000001"})  # a patron card tried at the login
    client.post(
        "/api/admin/cards", json={"uid": "STAFFSECRET01", "kind": "patron", "member_ref": "M0001"}, headers=admin
    )
    aud = login_card(client, "STAFFAUDIT01")
    log = client.get("/api/audit/events", params={"limit": 100}, headers=aud).json()["events"]
    text = str(log)
    for whole in ("NOSUCHCARD99", "PATRON000001", "STAFFSECRET01", "STAFFADMIN01"):
        assert whole not in text, f"{whole} must not be stored in full in the audit log"
    assert "NOS…D99" in text and "STA…T01" in text, "but the entries still say which card"
    export = client.get("/api/audit/export.csv", headers=aud).text
    assert "NOSUCHCARD99" not in export


# ---------- headers ----------
def test_every_response_carries_the_safe_headers(client, admin):
    for r in (
        client.get("/health"),
        client.get("/api/auth/me", headers=admin),
        client.get("/opac"),
        client.get("/static/app.css"),
    ):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "strict-transport-security" not in r.headers, "HSTS only means something over HTTPS"
    behind_proxy = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert "max-age" in behind_proxy.headers["strict-transport-security"]


def test_session_cookies_are_secure_in_production(settings):
    prod = dataclasses.replace(settings, environment="production", secret_key="k" * 40)
    from fastapi.testclient import TestClient

    from aisys.cli import seed_demo

    app = create_app(prod)  # creates the tables
    seed_demo(prod)
    with TestClient(app) as c:
        r = c.post("/api/auth/card-login", json={"uid": "STAFFADMIN01"})
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


# ---------- encrypted transport: a real HTTPS server ----------
def self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_application_serves_https_when_a_certificate_is_configured(settings, tmp_path):
    cert, key = self_signed(tmp_path)
    port = free_port()
    cfg = dataclasses.replace(settings, tls_certfile=str(cert), tls_keyfile=str(key), host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), **{**serve_options(cfg), "log_level": "warning"}))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "the HTTPS server did not start"
        trusting = ssl.create_default_context(cafile=str(cert))
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=trusting, timeout=10) as r:  # noqa: S310
            assert r.status == 200 and "max-age" in r.headers["Strict-Transport-Security"]
            assert r.headers["X-Frame-Options"] == "DENY"
        with pytest.raises(urllib.error.URLError):  # a client that does not trust the certificate is refused
            urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=ssl.create_default_context(), timeout=10)  # noqa: S310
        with pytest.raises(
            (urllib.error.URLError, ConnectionError, OSError)
        ):  # plain HTTP to the TLS port gets nothing
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)  # noqa: S310
        used = trusting.wrap_socket(
            socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname="127.0.0.1"
        )
        assert used.version() in ("TLSv1.2", "TLSv1.3"), "modern TLS only"
        used.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_tls_settings_are_checked_before_starting(settings, tmp_path):
    cert, key = self_signed(tmp_path)
    assert serve_options(settings)["host"] == settings.host and "ssl_certfile" not in serve_options(settings)
    with pytest.raises(SystemExit, match="both"):
        serve_options(dataclasses.replace(settings, tls_certfile=str(cert)))
    with pytest.raises(SystemExit, match="not found"):
        serve_options(dataclasses.replace(settings, tls_certfile=str(tmp_path / "no.pem"), tls_keyfile=str(key)))
    ok = serve_options(dataclasses.replace(settings, tls_certfile=str(cert), tls_keyfile=str(key)))
    assert ok["ssl_certfile"] == str(cert) and ok["ssl_keyfile"] == str(key)


def test_tls_comes_from_the_environment_too(tmp_path, monkeypatch):
    from aisys.config import load_settings

    monkeypatch.setenv("AISYS_TLS_CERTFILE", "/etc/aisys/cert.pem")
    monkeypatch.setenv("AISYS_TLS_KEYFILE", "/etc/aisys/key.pem")
    s = load_settings(str(tmp_path / "none.toml"))
    assert s.tls_certfile == "/etc/aisys/cert.pem" and s.tls_keyfile == "/etc/aisys/key.pem"
    monkeypatch.setenv("AISYS_TLS_CERTFILE", "")
    assert load_settings(str(tmp_path / "none.toml")).tls_certfile == "", "an empty value switches it off"


# ---------- the environment doctor ----------
def statuses(results):
    return {check: status for status, check, _ in results}


def test_a_good_environment_passes(settings):
    results = doctor.run(settings)
    s = statuses(results)
    assert s["Python"] == "OK" and s["SQLite"] == "OK" and s["Backup folder"] == "OK" and s["Database folder"] == "OK"
    assert "FAIL" not in s.values()
    assert "Ready to run." in doctor.render(results)


def test_the_doctor_names_what_is_wrong(settings, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    bad = dataclasses.replace(
        settings,
        backup_dir=str(blocker / "backups"),  # a folder cannot be made inside a file
        environment="production",
        secret_key="short",
        tls_certfile="only-one.pem",
    )
    s = statuses(doctor.run(bad))
    assert s["Backup folder"] == "FAIL" and s["Secret key"] == "FAIL" and s["TLS"] == "FAIL"
    assert "problem(s) to fix" in doctor.render(doctor.run(bad))


def test_the_doctor_notices_a_busy_port_and_plain_http_in_production(settings):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        cfg = dataclasses.replace(
            settings, host="127.0.0.1", port=busy.getsockname()[1], environment="production", secret_key="k" * 40
        )
        s = statuses(doctor.run(cfg))
    assert s["Port"] == "WARN" and s["TLS"] == "WARN" and s["Secret key"] == "OK"


def test_the_doctor_command_sets_its_exit_code(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AISYS_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("AISYS_BACKUP_DIR", settings.backup_dir)
    assert cli_main(["doctor"]) == 0
    assert "Ready to run." in capsys.readouterr().out
    monkeypatch.setenv("AISYS_TLS_CERTFILE", "half.pem")
    assert cli_main(["doctor"]) == 1
    assert "problem(s) to fix" in capsys.readouterr().out


# ---------- hostile input that used to be stored or coerced ----------
def test_a_nul_character_is_refused_in_any_json_body(client, admin):
    """D-079: NUL was stored in names, notes and titles and broke the exports downstream."""
    lib = login_card(client, "STAFFLIB0001")
    r = client.post("/api/circulation/members", json={"name": "a\u0000b"}, headers=lib)
    assert r.status_code == 422 and "NUL" in r.text
    raw = client.post(
        "/api/admin/roles", content=b'{"name":"nul_role","description":"x\\u0000","permissions":["catalogue.read"]}',
        headers={**admin, "content-type": "application/json"},
    )  # fmt: skip
    assert raw.status_code == 422
    assert client.post("/api/circulation/members", json={"name": "Ann Ok"}, headers=lib).status_code == 201


def test_money_and_quantities_must_be_real_whole_numbers(client):
    """D-080: true was taken as 1 cent and "100" as an amount."""
    lib = login_card(client, "STAFFLIB0001")
    for bad in (True, "100", 1.5):
        r = client.post("/api/circulation/members/M0005/fines/payment", json={"amount_cents": bad}, headers=lib)
        assert r.status_code == 422, bad
    line = {"title": "T", "quantity": True}
    assert client.post("/api/acquisitions", json={"supplier": "S", "lines": [line]}, headers=lib).status_code == 422
