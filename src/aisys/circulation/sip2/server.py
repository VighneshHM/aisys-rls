"""A small SIP2 server that stands in for an institution's ILMS (a simulator, never for production).

It listens on a real TCP port, speaks SIP2 2.00 (login, status, patron status and information, checkout, checkin,
renew, item information, resend), keeps its own patrons, items and loans, and applies the same simple loan rules as the
mock ILMS. With `autocreate` (the default) it accepts any member or item number it has not seen, so it can sit behind
the application with no set-up; tests switch that off and add exactly the people and books they need.

Failure injection (`fail_next`) breaks the next request after login in a chosen way, so tests and demos can prove how
the application copes:
- `drop`: the connection closes before anything is done;
- `lost_reply`: the work is done, the answer is never sent;
- `silent`: no answer, the connection stays open;
- `garble`: the answer arrives with a wrong checksum.
Run it with `python -m aisys.cli sip2-sim`.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aisys.circulation.connector import CirculationConfig
from aisys.circulation.sip2 import codec

FAIL_MODES = ("drop", "lost_reply", "silent", "garble")


@dataclass
class Patron:
    name: str = ""
    fines_cents: int = 0
    blocked: bool = False
    valid: bool = True
    charged: list[str] = field(default_factory=list)


@dataclass
class Item:
    title: str = ""
    charged_to: str = ""
    due: datetime | None = None
    renewals: int = 0


class _Handler(socketserver.BaseRequestHandler):
    server: _Tcp

    def handle(self) -> None:
        sim = self.server.sim
        sock: socket.socket = self.request
        sock.settimeout(60)
        sim.track(sock)
        logged_in = not sim.users
        buf = b""
        last_reply = b""
        while not sim.stopping.is_set():
            try:
                chunk = sock.recv(1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            if len(buf) > codec.MAX_FRAME:
                return  # not a SIP2 client
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                line = line.lstrip(b"\n")
                if not line:
                    continue
                reply, logged_in, close, keep = sim.respond(line, logged_in, last_reply)
                if reply is None:
                    if close:
                        return
                    continue
                last_reply = keep or reply  # a garbled reply must not become the one a resend gets back
                try:
                    sock.sendall(reply)
                except OSError:
                    return
                if close:
                    return


class _Tcp(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, sim: Sip2Server):
        super().__init__(address, _Handler)
        self.sim = sim


class Sip2Server:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        config: CirculationConfig | None = None,
        users: dict[str, str] | None = None,
        institution: str = "LIBRARY",
        autocreate: bool = True,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or CirculationConfig()
        self.users = users or {}  # login user -> password; empty means no login is needed
        self.institution = institution
        self.autocreate = autocreate
        self.now = now or (lambda: datetime.now(UTC))
        self.patrons: dict[str, Patron] = {}
        self.items: dict[str, Item] = {}
        self.received: list[tuple[str, str]] = []  # (command, raw text) of every request
        self.stopping = threading.Event()
        self._lock = threading.RLock()
        self._injected: list[str] = []
        self._conns: list[socket.socket] = []
        self._tcp = _Tcp((host, port), self)
        address = self._tcp.server_address
        self.host, self.port = str(address[0]), int(address[1])
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle and controls
    def start(self) -> Sip2Server:
        self._thread = threading.Thread(target=self._tcp.serve_forever, name="sip2-sim", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        self._tcp.shutdown()
        self._tcp.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> Sip2Server:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def add_patron(self, member_no: str, name: str = "", **kw) -> Patron:
        with self._lock:
            self.patrons[member_no] = Patron(name or member_no, **kw)
            return self.patrons[member_no]

    def add_item(self, accession_no: str, title: str = "") -> Item:
        with self._lock:
            self.items[accession_no] = Item(title or f"Item {accession_no}")
            return self.items[accession_no]

    def fail_next(self, mode: str = "drop", times: int = 1) -> None:
        if mode not in FAIL_MODES:
            raise ValueError(f"unknown failure mode '{mode}'")
        with self._lock:
            self._injected.extend([mode] * times)

    def close_connections(self) -> None:
        """Close every open client connection, as an idle timeout or a firewall would."""
        with self._lock:
            for conn in list(self._conns):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            self._conns.clear()

    def track(self, conn: socket.socket) -> None:
        with self._lock:
            self._conns.append(conn)

    def clear_failures(self) -> None:
        with self._lock:
            self._injected.clear()

    def count(self, command: str) -> int:
        return sum(1 for code, _ in self.received if code == command)

    # ------------------------------------------------------------------ one request -> one reply
    def respond(self, line: bytes, logged_in: bool, last_reply: bytes) -> tuple[bytes | None, bool, bool, bytes | None]:
        """(reply or None, now logged in, close afterwards, the clean reply to keep for a resend request)."""
        try:
            msg = codec.decode(line)
        except codec.Sip2Error:
            return self._wire("96", "", [], None), logged_in, False, None  # ask the other side to send it again
        with self._lock:
            self.received.append((msg.code, line.decode("ascii", "replace")))
            seq = msg.seq
            if msg.code == "97":
                return (last_reply or self._wire("96", "", [], seq)), logged_in, False, None
            if msg.code == "93":
                ok = not self.users or self.users.get(msg.get("CN")) == msg.get("CO")
                return self._wire("94", "1" if ok else "0", [], seq), logged_in or ok, not ok, None
            if not logged_in:
                return None, False, True, None  # a server that wants a login drops anyone who skips it
            mode = self._injected.pop(0) if self._injected else ""
            if mode == "drop":
                return None, logged_in, True, None
            if mode == "silent":
                return None, logged_in, False, None
            handler = {
                "99": self._status,
                "23": self._patron_status,
                "63": self._patron_info,
                "11": self._checkout,
                "29": self._renew,
                "09": self._checkin,
                "17": self._item_info,
            }.get(msg.code)
            if handler is None:
                return self._wire("96", "", [], seq), logged_in, False, None
            code, fixed, fields = handler(msg)
            reply = self._wire(code, fixed, fields, seq)
            if mode == "lost_reply":
                return None, logged_in, True, None  # done, but the answer is never sent
            if mode == "garble":
                text = reply.decode("ascii")
                bad = (text[:-5] + ("0" if text[-5] != "0" else "1") + text[-4:]).encode("ascii")
                return bad, logged_in, False, reply
            return reply, logged_in, False, None

    def _wire(self, code: str, fixed: str, fields: list[tuple[str, str]], seq: int | None) -> bytes:
        return codec.encode(code, fixed, fields, seq=seq)

    # ------------------------------------------------------------------ people and books
    def _patron(self, member_no: str) -> Patron | None:
        p = self.patrons.get(member_no)
        if p is None and self.autocreate and member_no:
            p = self.patrons[member_no] = Patron(member_no)
        return p

    def _item(self, accession_no: str) -> Item | None:
        it = self.items.get(accession_no)
        if it is None and self.autocreate and accession_no:
            it = self.items[accession_no] = Item(f"Item {accession_no}")
        return it

    def _flags(self, p: Patron) -> str:
        f = [" "] * 14
        if p.blocked:
            f[0] = "Y"  # charge privileges denied
        if len(p.charged) >= self.config.max_loans:
            f[5] = "Y"  # too many items charged
        if p.fines_cents > self.config.fine_limit_cents:
            f[10] = "Y"  # excessive outstanding fines
        return "".join(f)

    def _refusal_for(self, p: Patron | None) -> str:
        if p is None or not p.valid:
            return "Invalid patron"
        if p.blocked:
            return "Patron blocked"
        if p.fines_cents > self.config.fine_limit_cents:
            return "Patron has excessive fees"
        return ""

    def _head(self, **extra: str) -> list[tuple[str, str]]:
        return [("AO", self.institution)] + list(extra.items())

    # ------------------------------------------------------------------ the commands
    def _status(self, msg: codec.Message):
        fixed = "YYYYNN" + "030" + "003" + codec.stamp(self.now()) + "2.00"
        return "98", fixed, [("AO", self.institution), ("AF", "ACS online")]

    def _patron_reply(self, member_no: str, code: str, summary: str = ""):
        p = self._patron(member_no)
        stamp = codec.stamp(self.now())
        if p is None or not p.valid:
            base = "Y" + " " * 13 + "001" + stamp
            fixed = base + "0000" * 6 if code == "64" else base
            return code, fixed, [("AO", self.institution), ("AA", member_no), ("AE", ""), ("BL", "N"), ("CQ", "N")]
        base = self._flags(p) + "001" + stamp
        counts = "".join(f"{n:04d}" for n in (0, 0, len(p.charged), 0, 0, 0))
        fixed = base + counts if code == "64" else base
        fields = [("AO", self.institution), ("AA", member_no), ("AE", p.name), ("BL", "Y"), ("CQ", "Y")]
        if p.fines_cents:
            fields.append(("BV", f"{p.fines_cents / 100:.2f}"))
        if code == "64" and summary[2:3] == "Y":
            fields += [("AU", acc) for acc in p.charged]
        return code, fixed, fields

    def _patron_status(self, msg: codec.Message):
        return self._patron_reply(msg.get("AA"), "24")

    def _patron_info(self, msg: codec.Message):
        return self._patron_reply(msg.get("AA"), "64", msg.fixed[21:31])

    def _fail(self, code: str, text: str, member_no: str, item_no: str, ok_flags: str = "0NNN"):
        fixed = ok_flags + codec.stamp(self.now())
        fields = [("AO", self.institution), ("AA", member_no), ("AB", item_no), ("AF", text)]
        return code, fixed, fields

    def _checkout(self, msg: codec.Message):
        member_no, item_no = msg.get("AA"), msg.get("AB")
        p, it = self._patron(member_no), self._item(item_no)
        refusal = self._refusal_for(p)
        if not refusal and it is None:
            refusal = "Item not found"
        elif not refusal and it is not None and it.charged_to:
            refusal = "Item already checked out"
        elif not refusal and p is not None and len(p.charged) >= self.config.max_loans:
            refusal = "Too many items charged"
        if refusal or p is None or it is None:
            return self._fail("12", refusal or "Invalid patron", member_no, item_no)
        due = self.now() + timedelta(days=self.config.loan_days)
        it.charged_to, it.due, it.renewals = member_no, due, 0
        p.charged.append(item_no)
        fields = self._head(AA=member_no, AB=item_no, AJ=it.title, AH=codec.stamp(due), AF="Item checked out")
        return "12", "1NNY" + codec.stamp(self.now()), fields

    def _renew(self, msg: codec.Message):
        member_no, item_no = msg.get("AA"), msg.get("AB")
        p, it = self._patron(member_no), self._item(item_no)
        if it is None or it.charged_to != member_no:
            return self._fail("30", "Item not checked out", member_no, item_no, "0NNN")
        if (refusal := self._refusal_for(p)) or it.renewals >= self.config.max_renewals:
            return self._fail("30", refusal or "Renewal not allowed: maximum renewals reached", member_no, item_no)
        it.renewals += 1
        it.due = self.now() + timedelta(days=self.config.loan_days)
        fields = self._head(AA=member_no, AB=item_no, AJ=it.title, AH=codec.stamp(it.due), AF="Item renewed")
        return "30", "1YNY" + codec.stamp(self.now()), fields

    def _checkin(self, msg: codec.Message):
        item_no = msg.get("AB")
        it = self._item(item_no)
        if it is None or not it.charged_to:
            fields = [("AO", self.institution), ("AB", item_no), ("AF", "Item not checked out")]
            return "10", "0NNN" + codec.stamp(self.now()), fields
        member_no = it.charged_to
        p = self.patrons.get(member_no)
        if p and item_no in p.charged:
            p.charged.remove(item_no)
        it.charged_to, it.due, it.renewals = "", None, 0
        fields = self._head(AB=item_no, AQ=self.config.ilms_location or "MAIN", AJ=it.title, AA=member_no)
        return "10", "1NNN" + codec.stamp(self.now()), fields + [("AF", "Item checked in")]

    def _item_info(self, msg: codec.Message):
        item_no = msg.get("AB")
        it = self._item(item_no)
        if it is None:
            return "18", "01" + "01" + "01" + codec.stamp(self.now()), [("AB", item_no), ("AF", "Item not found")]
        fields: list[tuple[str, str]] = [("AB", item_no), ("AJ", it.title)]
        if it.charged_to and it.due:
            fields.append(("AH", codec.stamp(it.due)))
        return "18", ("04" if it.charged_to else "03") + "02" + "01" + codec.stamp(self.now()), fields
