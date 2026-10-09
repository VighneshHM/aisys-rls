"""A SIP2 session over TCP: connect, log in, send one message at a time, read the answer.

The session is opened the first time it is needed (so the application starts even when the ILMS is down) and is
dropped on any trouble; the next request opens a fresh one and logs in again. One request is outstanding at a time.

What the caller sees:
* `ConnectorError`  - nothing was sent (cannot connect, login refused). Safe to try again.
* `Sip2Unsure`      - the request was sent but no usable answer came back (timeout, connection dropped, garbled
                      beyond repair). The ILMS may or may not have acted: the connector checks before repeating.
When error detection is on, a damaged answer is re-requested with `97` (up to `RESENDS` times) and an ACS that asks for
a repeat with `96` gets the same message again, as the SIP2 specification describes.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from aisys.circulation.connector import ConnectorError, ConnectorTimeout
from aisys.circulation.sip2 import codec

log = logging.getLogger("aisys.sip2")

RESENDS = 2
Opener = Callable[[], socket.socket]


class Sip2Unsure(ConnectorTimeout):
    """The request left this computer but its outcome is unknown."""


class Sip2Client:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        login_user: str = "",
        login_password: str = "",
        location: str = "",
        institution: str = "",
        terminal_password: str = "",
        timeout_s: float = 5.0,
        checksum: bool = True,
        opener: Opener | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.login_user, self.login_password, self.location = login_user, login_password, location
        self.institution, self.terminal_password = institution, terminal_password
        self.timeout_s, self.checksum = timeout_s, checksum
        self._opener = opener or (lambda: socket.create_connection((host, port), timeout=timeout_s))
        self._sock: socket.socket | None = None
        self._seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ session
    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:  # already gone
                pass

    def _open(self) -> None:
        try:
            sock = self._opener()
            sock.settimeout(self.timeout_s)
        except TimeoutError as exc:
            raise ConnectorTimeout(f"the ILMS did not answer at {self.host}:{self.port}") from exc
        except OSError as exc:
            raise ConnectorError(f"cannot connect to the ILMS at {self.host}:{self.port}: {exc}") from exc
        self._sock = sock
        if not self.login_user:
            return  # many servers need no login
        fields = [("CN", self.login_user), ("CO", self.login_password)]
        if self.location:
            fields.append(("CP", self.location))
        try:
            reply = self._exchange("93", "00", fields, expect="94")
        except Sip2Unsure as exc:  # nothing was changed by logging in, so this is an ordinary failure
            self.close()
            raise ConnectorError(f"the ILMS did not answer the login: {exc}") from exc
        if reply.fixed[:1] != "1":
            self.close()
            raise ConnectorError("the ILMS refused the login (check the user name and the password)")

    # ------------------------------------------------------------------ one request
    def request(self, code: str, fixed: str, fields: list[tuple[str, str]], *, expect: str) -> codec.Message:
        """Send one message and return the answer, opening and logging in first when there is no session."""
        with self._lock:
            if self._sock is None:
                self._open()
            try:
                return self._exchange(code, fixed, fields, expect=expect)
            except Sip2Unsure:
                self.close()  # the next request starts clean
                raise

    def _next_seq(self) -> int | None:
        if not self.checksum:
            return None
        self._seq = (self._seq + 1) % 10
        return self._seq

    def _exchange(self, code: str, fixed: str, fields: list[tuple[str, str]], *, expect: str) -> codec.Message:
        seq = self._next_seq()
        try:
            wire = codec.encode(code, fixed, fields, seq=seq)
        except codec.Sip2Error as exc:
            raise ConnectorError(f"cannot build the {code} message: {exc}") from exc
        sent = wire
        for attempt in range(RESENDS + 1):
            try:
                self._send(sent)
                reply = self._read(seq)
            except codec.Sip2Error as exc:
                log.warning("SIP2 answer to %s was damaged (%s)", code, exc)
                if attempt >= RESENDS:
                    raise Sip2Unsure(f"the ILMS answer kept arriving damaged: {exc}") from exc
                sent = codec.encode("97", "", [], seq=seq)  # ask for the last answer again
                continue
            if reply.code == "96":  # the ILMS did not understand us: send the same message again
                if attempt >= RESENDS:
                    raise Sip2Unsure("the ILMS kept asking for the message to be sent again")
                sent = wire
                continue
            if reply.code != expect:
                raise Sip2Unsure(f"expected a {expect} answer to {code}, got {reply.code}")
            return reply
        raise Sip2Unsure("no usable answer")  # pragma: no cover - the loop always returns or raises

    def _send(self, wire: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(wire)
        except TimeoutError as exc:
            raise Sip2Unsure("timed out sending to the ILMS") from exc
        except OSError as exc:
            raise Sip2Unsure(f"the connection to the ILMS broke while sending: {exc}") from exc

    def _read(self, seq: int | None) -> codec.Message:
        """One line from the ILMS, checked. Raises Sip2Error for a damaged line, Sip2Unsure when nothing usable came."""
        assert self._sock is not None
        buf = bytearray()
        deadline = time.monotonic() + self.timeout_s  # for the WHOLE answer, so a trickle of bytes cannot stretch it
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Sip2Unsure("the ILMS did not finish its answer in time")
            try:
                self._sock.settimeout(left)
                chunk = self._sock.recv(1024)
            except TimeoutError as exc:
                raise Sip2Unsure("the ILMS did not answer in time") from exc
            except OSError as exc:
                raise Sip2Unsure(f"the connection to the ILMS broke: {exc}") from exc
            if not chunk:
                raise Sip2Unsure("the ILMS closed the connection without answering")
            buf += chunk
            if b"\r" in buf:
                break
            if len(buf) > codec.MAX_FRAME:
                raise codec.Sip2Error("the answer is too long")
        line = bytes(buf).split(b"\r", 1)[0].lstrip(b"\n")
        message = codec.decode(line)  # a checksum that is present must be right; a server may leave it off
        if message.protected and seq is not None and message.seq is not None and message.seq != seq:
            raise codec.Sip2Error(f"the answer has sequence {message.seq}, expected {seq}")
        return message
