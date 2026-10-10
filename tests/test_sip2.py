"""FR03 / AC03 / AC04: the SIP2 connector talking to a SIP2 server over a real TCP socket.

The server is the simulator in `aisys.circulation.sip2.server` (its own patrons, items and loans). Every test here sends
real SIP2 text over a real connection: the same rules as the mock, but through the wire protocol, the retry logic, and
the failure modes of a network (timeouts, dropped connections, lost answers, damaged answers, hostile servers).
"""

from __future__ import annotations

import dataclasses
import socket
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aisys import doctor
from aisys.app import create_app
from aisys.circulation.connector import (
    CirculationConfig,
    CirculationConnector,
    ConnectorError,
    ConnectorTimeout,
    ItemView,
    MemberView,
)
from aisys.circulation.registry import ConnectorConfigError, build_connector
from aisys.circulation.resilience import ResilientConnector
from aisys.circulation.sip2 import codec
from aisys.circulation.sip2.adapter import Sip2Connector
from aisys.circulation.sip2.client import RESENDS, Sip2Client, Sip2Unsure
from aisys.circulation.sip2.server import Sip2Server
from aisys.cli import seed_demo
from aisys.config import load_settings
from conftest import login_card
from test_circulation import book, events, out, renew

pytestmark = pytest.mark.req("FR03", "AC03", "AC04")

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
RULES = CirculationConfig(max_loans=2, max_renewals=1, loan_days=14, fine_limit_cents=500)


def member(no="M0001", **kw) -> MemberView:
    return MemberView(no, kw.pop("status", "active"), kw.pop("fines_cents", 0), kw.pop("open_loans", 0))


def item(no="A1", on_loan=False, due=None) -> ItemView:
    return ItemView(no, on_loan, 0, due)


def config_for(server: Sip2Server, **kw) -> CirculationConfig:
    base = dict(
        ilms_driver="sip2",
        ilms_endpoint=server.host,
        ilms_port=server.port,
        ilms_institution="LIBRARY",
        ilms_location="MAIN",
        ilms_timeout_s=0.5,
        ilms_backoff_s=0.0,
        ilms_retries=2,
    )
    base.update(kw)
    return dataclasses.replace(RULES, **base)


@pytest.fixture
def sim():
    with Sip2Server(config=RULES, autocreate=False) as server:
        server.add_patron("M0001", "Priya")
        server.add_patron("M0002", "Blocked Ben", blocked=True)
        server.add_patron("M0003", "Owes Olu", fines_cents=900)
        for acc in ("A1", "A2", "A3", "A4"):
            server.add_item(acc, f"Book {acc}")
        yield server


@pytest.fixture
def conn(sim):
    c = Sip2Connector(config_for(sim))
    yield c
    c.close()


@pytest.fixture
def resilient(sim):
    c = Sip2Connector(config_for(sim))
    yield ResilientConnector(c, retries=2, backoff_s=0.0)
    c.close()


# ================================================================== the contract: same answers as the mock
def test_it_is_a_circulation_connector(conn):
    assert isinstance(conn, CirculationConnector) and conn.protocol == "sip2"


def test_patron_status_ok_blocked_fines_and_unknown(conn):
    assert conn.patron_status("t1", member("M0001")).ok
    assert conn.patron_status("t2", member("M0002")).code == "patron_blocked"
    assert conn.patron_status("t3", member("M0003")).code == "fines_exceeded"
    assert conn.patron_status("t4", member("M9999")).code == "patron_blocked"  # the ILMS does not know them


def test_checkout_lends_the_book_and_returns_the_ilms_due_date(conn, sim):
    r = conn.checkout("c1", member(), item("A1"), now=NOW)
    assert r.ok and r.code == "ok" and r.protocol == "sip2" and not r.replayed
    assert r.due_at is not None and abs((r.due_at - datetime.now(UTC)).days - 13) <= 1  # 14 days from the ILMS's clock
    assert sim.items["A1"].charged_to == "M0001" and sim.patrons["M0001"].charged == ["A1"]


def test_checkout_refusals_use_the_neutral_codes(conn, sim):
    assert conn.checkout("c1", member("M0002"), item("A1"), now=NOW).code == "patron_blocked"
    assert conn.checkout("c2", member("M0003"), item("A1"), now=NOW).code == "fines_exceeded"
    assert conn.checkout("c3", member("M9999"), item("A1"), now=NOW).code == "patron_blocked"
    assert conn.checkout("c4", member(), item("A1"), now=NOW).ok
    r = conn.checkout("c5", member("M0001"), item("A1"), now=NOW)  # already out
    assert (r.ok, r.code) == (False, "item_on_loan") and "already" in r.message.lower()
    assert conn.checkout("c6", member(), item("A2"), now=NOW).ok
    r = conn.checkout("c7", member(), item("A3"), now=NOW)  # two is the limit
    assert (r.ok, r.code) == (False, "loan_limit")
    assert sim.items["A3"].charged_to == ""  # a refusal changes nothing


def test_an_item_the_ilms_does_not_know_is_refused_in_the_ilms_words(conn):
    r = conn.checkout("c1", member(), item("NOPE"), now=NOW)
    assert (r.ok, r.code, r.message) == (False, "ilms_refused", "Item not found")


def test_renew_extends_then_hits_the_limit(conn, sim):
    first = conn.checkout("c1", member(), item("A1"), now=NOW)
    r = conn.renew("r1", member(), item("A1", True, first.due_at), now=NOW)
    assert r.ok and sim.items["A1"].renewals == 1
    r2 = conn.renew("r2", member(), item("A1", True, r.due_at), now=NOW)
    assert (r2.ok, r2.code) == (False, "renewal_limit")
    assert sim.items["A1"].renewals == 1


def test_renew_of_a_book_that_is_not_out(conn):
    r = conn.renew("r1", member(), item("A1"), now=NOW)
    assert (r.ok, r.code) == (False, "not_on_loan")


def test_checkin_and_a_second_checkin(conn, sim):
    conn.checkout("c1", member(), item("A1"), now=NOW)
    assert conn.checkin("i1", item("A1", True), now=NOW).ok
    assert sim.items["A1"].charged_to == "" and sim.patrons["M0001"].charged == []
    r = conn.checkin("i2", item("A1"), now=NOW)
    assert (r.ok, r.code) == (False, "not_on_loan")


def test_due_date_of_a_loan_and_of_a_book_that_is_in(conn):
    assert conn.due_date("d1", item("A1")).code == "not_on_loan"
    out_ = conn.checkout("c1", member(), item("A1"), now=NOW)
    r = conn.due_date("d2", item("A1", True))
    assert r.ok and r.due_at == out_.due_at.replace(microsecond=0)


def test_a_repeat_of_a_finished_request_gets_the_same_answer_and_is_not_sent_again(conn, sim):
    a = conn.checkout("same", member(), item("A1"), now=NOW)
    b = conn.checkout("same", member(), item("A1"), now=NOW)
    assert a.ok and b.ok and b.replayed and b.due_at == a.due_at
    assert sim.count("11") == 1 and sim.patrons["M0001"].charged == ["A1"]


def test_a_refusal_is_remembered_too(conn, sim):
    a = conn.checkout("x", member("M0002"), item("A1"), now=NOW)
    b = conn.checkout("x", member("M0002"), item("A1"), now=NOW)
    assert not a.ok and b.code == a.code and b.replayed and sim.count("11") == 1


# ================================================================== the wire: login, checksum, sequence
def test_requests_carry_a_sequence_digit_and_a_checksum(conn, sim):
    conn.patron_status("t1", member())
    conn.patron_status("t2", member("M0003"))
    texts = [t for c, t in sim.received if c == "23"]
    assert len(texts) == 2
    seqs = []
    for t in texts:
        m = codec.decode(t)
        assert m.protected and m.get("AO") == "LIBRARY" and m.get("AA").startswith("M000")
        seqs.append(m.seq)
    assert seqs[0] != seqs[1]  # a new digit for each message


def test_without_error_detection_no_trailer_is_sent(sim):
    c = Sip2Connector(config_for(sim, ilms_checksum=False))
    try:
        assert c.patron_status("t1", member()).ok
    finally:
        c.close()
    assert all("AY" not in t.split("|")[-1] for _, t in sim.received)


def test_login_is_sent_first_and_once_per_connection():
    with Sip2Server(config=RULES, users={"desk": "pw"}) as srv:
        c = Sip2Connector(config_for(srv, ilms_login="desk", ilms_password="pw"))
        try:
            assert c.patron_status("t1", member()).ok
            assert c.checkout("t2", member(), item("A1"), now=NOW).ok
            assert [code for code, _ in srv.received][:2] == ["93", "23"] and srv.count("93") == 1
            login = codec.decode(srv.received[0][1])
            assert (login.get("CN"), login.get("CO"), login.get("CP")) == ("desk", "pw", "MAIN")
        finally:
            c.close()


def test_a_refused_login_says_so_and_changes_nothing():
    with Sip2Server(config=RULES, users={"desk": "pw"}) as srv:
        c = Sip2Connector(config_for(srv, ilms_login="desk", ilms_password="wrong"))
        try:
            with pytest.raises(ConnectorError, match="refused the login"):
                c.checkout("t1", member(), item("A1"), now=NOW)
            assert srv.count("11") == 0
        finally:
            c.close()


def test_a_server_that_needs_a_login_drops_a_client_that_does_not_log_in():
    with Sip2Server(config=RULES, users={"desk": "pw"}) as srv:
        c = Sip2Connector(config_for(srv))  # no login configured
        try:
            with pytest.raises(ConnectorError):
                c.patron_status("t1", member())
        finally:
            c.close()


def test_nothing_listening_is_a_connector_error_not_a_crash():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # a port that nothing is listening on
    c = Sip2Connector(
        CirculationConfig(ilms_driver="sip2", ilms_endpoint="127.0.0.1", ilms_port=port, ilms_timeout_s=0.5)
    )
    with pytest.raises(
        ConnectorError, match="cannot connect|did not answer"
    ):  # Windows reports a closed port as a timeout
        c.patron_status("t1", member())


# ================================================================== failures on the network
def test_a_server_that_never_answers_times_out_and_the_request_was_not_processed(conn, sim):
    sim.fail_next("silent")
    with pytest.raises(ConnectorTimeout):
        conn.checkout("c1", member(), item("A1"), now=NOW)
    assert sim.items["A1"].charged_to == ""
    r = conn.checkout("c1", member(), item("A1"), now=NOW)  # the same transaction again
    assert r.ok and sim.patrons["M0001"].charged == ["A1"]


def test_a_dropped_connection_is_re_opened_for_the_retry(resilient, sim):
    sim.fail_next("drop")
    r = resilient.checkout("c1", member(), item("A1"), now=NOW)
    assert r.ok and sim.count("11") == 2 and sim.patrons["M0001"].charged == ["A1"]


def test_a_lost_checkout_answer_does_not_lend_the_book_twice(resilient, sim):
    sim.fail_next("lost_reply")  # the ILMS lends the book, the answer never arrives
    r = resilient.checkout("c1", member(), item("A1"), now=NOW)
    assert r.ok and r.replayed and r.due_at is not None
    assert sim.count("11") == 1  # the retry checked first and did not send a second checkout
    assert sim.patrons["M0001"].charged == ["A1"] and sim.items["A1"].renewals == 0


def test_a_lost_checkin_answer_is_recognised_on_retry(resilient, sim):
    sim.add_patron("M0001").charged.append("A1")
    sim.items["A1"].charged_to = "M0001"
    sim.fail_next("lost_reply")
    r = resilient.checkin("i1", item("A1", True), now=NOW)
    assert r.ok and r.replayed
    assert sim.items["A1"].charged_to == "" and sim.count("09") == 2


def test_a_lost_renew_answer_does_not_renew_twice(resilient, sim):
    first = resilient.checkout("c1", member(), item("A1"), now=NOW)
    sim.items["A1"].due = datetime.now(UTC) + timedelta(days=1)  # so a renewal visibly moves the date
    old_due = sim.items["A1"].due.replace(microsecond=0)
    sim.fail_next("lost_reply")
    r = resilient.renew("r1", member(), item("A1", True, old_due), now=NOW)
    assert r.ok and r.replayed and r.due_at != old_due
    assert sim.items["A1"].renewals == 1 and sim.count("29") == 1
    assert first.ok


def test_a_lost_answer_is_not_mistaken_for_success_when_the_book_went_to_someone_else(resilient, sim):
    sim.patrons["M0003"].fines_cents = 0
    sim.add_patron("M0004")
    assert resilient.checkout("a", member("M0004"), item("A1"), now=NOW).ok  # someone else has it
    sim.fail_next("drop")  # this request is never processed
    r = resilient.checkout("b", member("M0001"), item("A1"), now=NOW)
    assert (r.ok, r.code) == (False, "item_on_loan")  # not charged to M0001, so no false success
    assert sim.items["A1"].charged_to == "M0004"


def test_a_damaged_answer_is_asked_for_again_with_97(conn, sim):
    sim.fail_next("garble")
    r = conn.checkout("c1", member(), item("A1"), now=NOW)
    assert r.ok and sim.count("97") == 1 and sim.count("11") == 1  # done once, the answer was re-sent
    assert sim.patrons["M0001"].charged == ["A1"]


def test_every_failure_with_retries_exhausted_is_one_clean_503_style_result(sim):
    c = ResilientConnector(Sip2Connector(config_for(sim)), retries=1, backoff_s=0.0)
    sim.fail_next("drop", 5)
    r = c.checkout("c1", member(), item("A1"), now=NOW)
    assert (r.ok, r.code) == (False, "ilms_unavailable") and "2 tries" in r.message
    assert sim.items["A1"].charged_to == ""


# ---- hostile or broken servers (a raw socket that answers however the test wants)
class Raw:
    """A one-purpose TCP server: `handler(conn_socket, request_bytes)` decides what to send back."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.seen: list[bytes] = []
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(2)
        try:
            while True:
                data = conn.recv(2048)
                if not data:
                    return
                self.seen.append(data)
                self.handler(conn, data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop = True
        self.sock.close()


@pytest.fixture
def raw():
    made = []

    def make(handler):
        r = Raw(handler)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def client_for(r: Raw, **kw) -> Sip2Client:
    return Sip2Client("127.0.0.1", r.port, timeout_s=0.5, **kw)


def ask(c: Sip2Client):
    return c.request("23", "001" + codec.stamp(), [("AO", "L"), ("AA", "M1"), ("AC", "")], expect="24")


def test_an_answer_with_a_wrong_checksum_every_time_ends_as_unsure(raw):
    def reply(conn, data):
        good = codec.encode("24", " " * 14 + "001" + codec.stamp(), [("AA", "M1")], seq=1).decode()
        conn.sendall((good[:-5] + "0" + good[-4:]).encode())  # always damaged

    r = raw(reply)
    with pytest.raises(Sip2Unsure, match="damaged"):
        ask(client_for(r))
    assert len(r.seen) == 1 + RESENDS  # the request, then only the permitted number of 97 resend requests


def test_an_answer_with_the_wrong_sequence_is_not_trusted(raw):
    def reply(conn, data):
        seq = codec.decode(data).seq
        conn.sendall(codec.encode("24", " " * 14 + "001" + codec.stamp(), [], seq=(seq + 3) % 10))

    with pytest.raises(Sip2Unsure):
        ask(client_for(raw(reply)))


def test_a_server_that_keeps_asking_for_a_repeat_is_given_up_on(raw):
    r = raw(lambda conn, data: conn.sendall(codec.encode("96", "", [])))
    with pytest.raises(Sip2Unsure, match="again"):
        ask(client_for(r))
    assert len(r.seen) == 1 + RESENDS


def test_the_wrong_kind_of_answer_is_refused(raw):
    def reply(conn, data):
        conn.sendall(codec.encode("94", "1", [], seq=codec.decode(data).seq))

    with pytest.raises(Sip2Unsure, match="expected a 24"):
        ask(client_for(raw(reply)))


@pytest.mark.parametrize(
    "garbage",
    [
        b"x" * 10_000,  # far longer than any message, never a carriage return
        b"\xff\xfe\xfd\r",  # not ASCII
        b"hello\r",  # not a message
        b"\r",  # empty line
        b"24\r",  # a command with no fixed part
        b"9300CN\x00x|\r",  # control characters
    ],
)
def test_garbage_from_the_server_never_crashes_the_client(raw, garbage):
    r = raw(lambda conn, data: conn.sendall(garbage))
    with pytest.raises(ConnectorError):
        ask(client_for(r))


def test_a_server_that_closes_without_answering_is_unsure(raw):
    with pytest.raises(Sip2Unsure, match="closed"):
        ask(client_for(raw(lambda conn, data: conn.close())))


def test_the_client_never_sends_a_value_that_could_inject_a_field(sim):
    c = Sip2Connector(config_for(sim))
    try:
        for bad in ("M0001|AB", "M0001\rAA", "Mé", "M0001\n"):
            with pytest.raises(ConnectorError, match="cannot build"):
                c.checkout("x" + bad, member(bad), item("A1"), now=NOW)
        assert sim.count("11") == 0 and sim.items["A1"].charged_to == ""  # nothing was sent
    finally:
        c.close()


def test_an_ilms_that_leaves_out_the_due_date_still_gets_one():
    class NoDue(Sip2Server):
        def _checkout(self, msg):
            code, fixed, fields = super()._checkout(msg)
            return code, fixed, [f for f in fields if f[0] != "AH"]

        def _item_info(self, msg):
            code, fixed, fields = super()._item_info(msg)
            return code, fixed, [f for f in fields if f[0] != "AH"]

    with NoDue(config=RULES) as srv:
        c = Sip2Connector(config_for(srv))
        try:
            r = c.checkout("c1", member(), item("A1"), now=NOW)
            assert r.ok and r.due_at == NOW + timedelta(days=RULES.loan_days)  # the local rule fills the gap
        finally:
            c.close()


def test_an_unfamiliar_refusal_text_falls_back_to_the_patron_flags():
    class Odd(Sip2Server):
        def _checkout(self, msg):
            code, fixed, fields = super()._checkout(msg)
            if fixed[0] == "0":
                fields = [("AF", "Computer says no") if f[0] == "AF" else f for f in fields]
            return code, fixed, fields

    with Odd(config=RULES) as srv:
        srv.add_patron("M0002", blocked=True)
        c = Sip2Connector(config_for(srv))
        try:
            r = c.checkout("c1", member("M0002"), item("A1"), now=NOW)
            assert (r.ok, r.code) == (False, "patron_blocked")  # the flags say why when the words do not
        finally:
            c.close()


# ================================================================== configuration
def test_the_driver_is_registered_and_needs_an_address():
    with pytest.raises(ConnectorConfigError, match="endpoint and port"):
        build_connector(CirculationConfig(ilms_driver="sip2"))
    with pytest.raises(ConnectorConfigError, match="endpoint and port"):
        build_connector(CirculationConfig(ilms_driver="sip2", ilms_endpoint="ils.example.org"))
    built = build_connector(CirculationConfig(ilms_driver="sip2", ilms_endpoint="ils.example.org", ilms_port=6001))
    assert isinstance(built, ResilientConnector) and built.protocol == "sip2"  # opening is lazy: no server needed yet


def test_the_settings_read_the_sip2_options_and_the_password_only_from_the_environment(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[ilms]\ndriver = "sip2"\nendpoint = "ils.example.org"\nport = 6001\nlogin = "aisys"\n'
        'password = "from-the-file"\ninstitution = "MAIN"\nlocation = "DESK1"\ntimeout_s = 3\nchecksum = false\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AISYS_ILMS_PASSWORD", raising=False)
    c = load_settings(cfg).circulation
    assert (c.ilms_driver, c.ilms_endpoint, c.ilms_port, c.ilms_login) == ("sip2", "ils.example.org", 6001, "aisys")
    assert (c.ilms_institution, c.ilms_location, c.ilms_timeout_s, c.ilms_checksum) == ("MAIN", "DESK1", 3.0, False)
    assert c.ilms_password == ""  # a password written in the file is ignored
    monkeypatch.setenv("AISYS_ILMS_PASSWORD", "from-the-environment")
    assert load_settings(cfg).circulation.ilms_password == "from-the-environment"


def test_the_password_never_shows_up_in_a_printout_of_the_settings():
    c = CirculationConfig(ilms_password="hunter2-secret")
    assert "hunter2-secret" not in repr(c) and "hunter2-secret" not in str(dataclasses.asdict(c).keys())


@pytest.mark.parametrize("bad", ["0", "0.01", "121", "'soon'", "-3"])
def test_a_silly_timeout_stops_the_application_starting_with_a_plain_message(tmp_path, bad):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"[ilms]\ntimeout_s = {bad}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="timeout_s"):
        load_settings(cfg)


# ================================================================== doctor and the simulator program
def test_doctor_reports_the_ilms(sim):
    s = dataclasses.replace(load_settings("nope.toml"), circulation=config_for(sim))
    (status, check, detail) = doctor._ilms_check(s)
    assert (status, check) == ("OK", "ILMS") and "online" in detail
    sim.stop()
    status, _, detail = doctor._ilms_check(s)
    assert status == "FAIL" and str(sim.port) in detail
    mock = dataclasses.replace(s, circulation=CirculationConfig())
    assert doctor._ilms_check(mock)[0] == "OK"
    unset = dataclasses.replace(s, circulation=CirculationConfig(ilms_driver="sip2"))
    assert doctor._ilms_check(unset)[0] == "FAIL"


def test_the_simulator_program_starts_and_answers():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "aisys.cli", "sip2-sim", "--port", str(port), "--user", "u", "--password", "p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert proc.stdout is not None and b"listening" in proc.stdout.readline()
        c = Sip2Connector(
            CirculationConfig(
                ilms_driver="sip2", ilms_endpoint="127.0.0.1", ilms_port=port, ilms_login="u", ilms_password="p"
            )
        )
        assert c.checkout("t1", member("M1"), item("B1"), now=NOW).ok
        assert c.status().fixed[:1] == "Y"
        c.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ================================================================== the whole application on the SIP2 driver
@pytest.fixture
def lib(sip_client):
    return login_card(sip_client, "STAFFLIB0001")


@pytest.fixture
def sip_sim():
    with Sip2Server(config=RULES, autocreate=True) as server:
        yield server


@pytest.fixture
def sip_client(settings, sip_sim):
    cfg = dataclasses.replace(settings, circulation=config_for(sip_sim, ilms_timeout_s=1.0))
    app = create_app(cfg)
    seed_demo(cfg)
    with TestClient(app) as c:
        c.settings = cfg  # type: ignore[attr-defined]
        yield c


def test_the_application_uses_the_sip2_driver_when_configured(sip_client):
    assert isinstance(sip_client.app.state.ilms.inner, Sip2Connector)
    assert sip_client.app.state.ilms.protocol == "sip2"


def test_check_out_renew_and_check_in_through_the_api_over_sip2(sip_client, lib, sip_sim):
    book(sip_client, lib, "W1")
    r = out(sip_client, lib, "M0001", "W1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["protocol"] == "sip2" and body["loan"]["member_no"] == "M0001"
    assert sip_sim.items["W1"].charged_to == "M0001"  # the ILMS really has the loan
    due1 = body["loan"]["due_at"]
    r = renew(sip_client, lib, "W1")
    assert r.status_code == 200, r.text
    assert sip_sim.items["W1"].renewals == 1
    r = sip_client.post("/api/circulation/checkin", headers=lib, json={"item_ref": "W1"})
    assert r.status_code == 200, r.text
    assert sip_sim.items["W1"].charged_to == ""
    kinds = [(e["action"], e["outcome"], e["protocol"]) for e in events(sip_client, lib)]
    assert (
        ("checkout", "ok", "sip2") in kinds and ("checkin", "ok", "sip2") in kinds and ("renew", "ok", "sip2") in kinds
    )
    assert due1


def test_an_ilms_refusal_reaches_the_desk_in_plain_words_and_nothing_is_recorded_locally(sip_client, lib, sip_sim):
    book(sip_client, lib, "W2")
    sip_sim.add_patron("M0001", "Priya", blocked=True)
    r = out(sip_client, lib, "M0001", "W2")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "patron_blocked"
    assert sip_sim.items["W2"].charged_to == ""
    assert sip_client.get("/api/circulation/loans", headers=lib).json()["loans"] == []


def test_an_ilms_outage_is_a_503_and_leaves_no_half_finished_loan(sip_client, lib, sip_sim):
    book(sip_client, lib, "W3")
    sip_sim.fail_next("drop", 6)
    r = out(sip_client, lib, "M0001", "W3")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "ilms_unavailable"
    assert sip_client.get("/api/circulation/loans", headers=lib).json()["loans"] == []
    sip_sim.clear_failures()
    assert out(sip_client, lib, "M0001", "W3").status_code == 200  # and it works again once the ILMS is back


def test_a_lost_answer_over_the_api_gives_one_loan_not_two(sip_client, lib, sip_sim):
    book(sip_client, lib, "W4")
    sip_sim.fail_next("lost_reply")
    r = out(sip_client, lib, "M0001", "W4")
    assert r.status_code == 200, r.text
    loans = sip_client.get("/api/circulation/loans", headers=lib).json()["loans"]
    assert len(loans) == 1 and sip_sim.count("11") == 1 and sip_sim.patrons["M0001"].charged == ["W4"]


# ================================================================== found while testing the wire path
def test_a_server_that_dribbles_bytes_cannot_hold_a_request_open_forever(raw):
    """One byte at a time, each inside the per-read timeout: the whole answer must still have a deadline."""
    import time

    def dribble(conn, data):
        for _ in range(60):
            conn.sendall(b"2")  # never a complete message, never quiet for long
            time.sleep(0.2)

    started = time.monotonic()
    with pytest.raises(ConnectorError):
        ask(client_for(raw(dribble)))  # timeout_s is 0.5
    assert time.monotonic() - started < 3, "the request was held open far longer than the timeout"


def test_a_patron_with_hundreds_of_loans_can_still_be_checked_for_a_retry(resilient, sim):
    sim.config = dataclasses.replace(sim.config, max_loans=1000)
    for n in range(600):  # the patron information answer is far longer than 4 KB
        sim.add_item(f"BULK-{n:04d}")
        sim.patrons["M0001"].charged.append(f"BULK-{n:04d}")
        sim.items[f"BULK-{n:04d}"].charged_to = "M0001"
    sim.fail_next("lost_reply")
    r = resilient.checkout("big", member(), item("A1"), now=NOW)
    assert r.ok and r.replayed and sim.count("11") == 1
    assert "A1" in sim.patrons["M0001"].charged


def test_many_desks_at_once_all_get_a_consistent_answer(sim):
    c = ResilientConnector(Sip2Connector(config_for(sim, ilms_timeout_s=3)), retries=2, backoff_s=0.0)
    sim.config = dataclasses.replace(sim.config, max_loans=100)
    for n in range(40):
        sim.add_item(f"P{n}")
    results: dict[int, object] = {}

    def desk(n: int) -> None:
        results[n] = c.checkout(f"par-{n}", member(f"M{n:04d}"), item(f"P{n}"), now=NOW)

    for n in range(40):
        sim.add_patron(f"M{n:04d}")
    threads = [threading.Thread(target=desk, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 40 and all(r.ok for r in results.values())
    assert all(sim.items[f"P{n}"].charged_to == f"M{n:04d}" for n in range(40))
    assert sim.count("11") == 40


def test_a_connection_the_ilms_closed_while_idle_is_replaced_without_a_wrong_answer(resilient, sim):
    assert resilient.patron_status("t1", member()).ok
    sim.close_connections()  # the ILMS (or a firewall) closed the idle session
    r = resilient.checkout("c1", member(), item("A1"), now=NOW)
    assert r.ok and sim.patrons["M0001"].charged == ["A1"] and sim.count("11") == 1


def test_the_memory_of_unsure_requests_cannot_grow_without_limit(sim):
    c = Sip2Connector(config_for(sim, ilms_timeout_s=0.2))
    try:
        for n in range(30):
            sim.fail_next("silent")
            with pytest.raises(ConnectorError):
                c.patron_status(f"never-retried-{n}", member())
        assert len(c._unsure) == 30  # fine at this size...
        from aisys.circulation.sip2 import adapter

        for n in range(adapter.REMEMBER + 50):
            c._unsure[f"x{n}"] = None
        sim.fail_next("silent")
        with pytest.raises(ConnectorError):
            c.patron_status("one-more", member())
        assert len(c._unsure) <= adapter.REMEMBER  # ...but bounded
    finally:
        c.close()


def test_a_book_the_ilms_already_has_back_can_still_be_closed_locally(sip_client, lib, sip_sim):
    """Returned at another desk in the library system: the local loan must not stay open for ever."""
    book(sip_client, lib, "W5")
    assert out(sip_client, lib, "M0001", "W5").status_code == 200
    sip_sim.items["W5"].charged_to = ""  # the ILMS was told about the return by another system
    sip_sim.patrons["M0001"].charged.remove("W5")
    r = sip_client.post("/api/circulation/checkin", headers=lib, json={"item_ref": "W5"})
    assert r.status_code == 200, r.text
    assert "already" in r.json()["message"].lower()
    assert sip_client.get("/api/circulation/loans", headers=lib).json()["loans"] == []
    assert sip_client.get("/api/circulation/loans", headers=lib, params={"open_only": False}).json()["loans"][0][
        "returned_at"
    ]
    again = sip_client.post("/api/circulation/checkin", headers=lib, json={"item_ref": "W5"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_on_loan"  # now it really is not on loan


def test_a_renewal_the_ilms_does_not_recognise_is_still_refused(sip_client, lib, sip_sim):
    book(sip_client, lib, "W6")
    assert out(sip_client, lib, "M0001", "W6").status_code == 200
    sip_sim.items["W6"].charged_to = ""
    sip_sim.patrons["M0001"].charged.remove("W6")
    r = renew(sip_client, lib, "W6")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_on_loan"
    assert len(sip_client.get("/api/circulation/loans", headers=lib).json()["loans"]) == 1  # nothing guessed


def test_a_member_removed_while_the_sip2_ilms_is_lending_gets_the_book_taken_back(sip_client, lib, sip_sim):
    """The ILMS really lent the book; the member was removed before it was recorded here: the ILMS must be undone."""
    book(sip_client, lib, "W7")
    inner = sip_client.app.state.ilms.inner
    real = inner.checkout

    def removed_meanwhile(txn_id, member, item, *, now):
        answer = real(txn_id, member, item, now=now)
        r = sip_client.post("/api/members/M0001/remove", headers=lib, json={"reason": "left"})
        assert r.status_code == 200, r.text
        return answer

    inner.checkout = removed_meanwhile
    try:
        r = out(sip_client, lib, "M0001", "W7")
    finally:
        inner.checkout = real
    assert r.status_code == 409 and r.json()["detail"]["code"] == "member_removed", r.text
    assert sip_sim.items["W7"].charged_to == "" and sip_sim.patrons["M0001"].charged == []  # taken back at the ILMS
    assert sip_client.get("/api/circulation/loans", headers=lib).json()["loans"] == []
