"""SIP2 messages: build them, read them, and protect them with the optional checksum (3M SIP version 2.00).

A SIP2 message is one line of text ending in a carriage return: a two-digit command, fixed-length fields in a set
order, then variable fields written as a two-letter id, a value and a `|`, for example `AAM0001|AB10042|`. When error
detection is on, the line ends `AY<n>AZ<hhhh>`: a sequence digit and a 16-bit checksum of everything before the
hex digits. This module is pure (no sockets, no clock), so every rule here is tested by feeding it text.

Nothing here trusts what it reads: a frame that is too long, has a stray control character, a wrong checksum or a
fixed part shorter than its command needs is refused with `Sip2Error`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

MAX_FRAME = 65536  # a patron information answer can list hundreds of items; anything longer is not a message
EOL = "\r"
DATE_WIDTH = 18  # YYYYMMDDZZZZHHMMSS


class Sip2Error(ValueError):
    """The text is not a valid SIP2 message (bad checksum, too short, forbidden characters...)."""


# command -> length of its fixed part (the characters between the two-digit command and the first variable field)
# Requests (SC to ACS) and responses (ACS to SC) as in the SIP2 specification.
FIXED_LENGTH: dict[str, int] = {
    "93": 2,  # login: UID algorithm, PWD algorithm
    "94": 1,  # login response: ok
    "99": 8,  # SC status: status code (1), max print width (3), protocol version (4)
    "98": 34,  # ACS status: 6 flags, timeout (3), retries (3), date (18), protocol version (4)
    "23": 21,  # patron status request: language (3), date (18)
    "24": 35,  # patron status response: status (14), language (3), date (18)
    "11": 38,  # checkout: renewal policy (1), no block (1), date (18), nb due date (18)
    "12": 22,  # checkout response: ok, renewal ok, magnetic, desensitize (4), date (18)
    "09": 37,  # checkin: no block (1), date (18), return date (18)
    "10": 22,  # checkin response: ok, resensitize, magnetic, alert (4), date (18)
    "29": 38,  # renew: third party (1), no block (1), date (18), nb due date (18)
    "30": 22,  # renew response: ok, renewal ok, magnetic, desensitize (4), date (18)
    "17": 18,  # item information: date (18)
    "18": 24,  # item information response: circulation status (2), security marker (2), fee type (2), date (18)
    "63": 31,  # patron information: language (3), date (18), summary (10)
    "64": 59,  # patron information response: status (14), language (3), date (18), six counts (4 each)
    "96": 0,  # ACS asks the SC to send the last message again
    "97": 0,  # SC asks the ACS to send the last message again
}

SC_REQUESTS = ("93", "99", "23", "11", "09", "29", "17", "63", "97")
ACS_RESPONSES = ("94", "98", "24", "12", "10", "30", "18", "64", "96")
RESPONSE_OF = {"93": "94", "99": "98", "23": "24", "11": "12", "09": "10", "29": "30", "17": "18", "63": "64"}


@dataclass(frozen=True)
class Message:
    """A parsed message. `fixed` is the raw fixed part; `fields` keeps every variable field in order."""

    code: str
    fixed: str = ""
    fields: tuple[tuple[str, str], ...] = ()
    seq: int | None = None  # the AY sequence digit, when the message carried error detection
    protected: bool = field(default=False, compare=False)

    def get(self, fid: str, default: str = "") -> str:
        for key, value in self.fields:
            if key == fid:
                return value
        return default

    def all(self, fid: str) -> list[str]:
        return [value for key, value in self.fields if key == fid]


def checksum(text: str) -> str:
    """The four hex digits that make the sum of all characters, plus the checksum, equal 0 modulo 65536."""
    total = sum(text.encode("ascii", "replace"))
    return f"{(-total) & 0xFFFF:04X}"


def stamp(when: datetime | None = None) -> str:
    """The 18-character transaction date: `YYYYMMDD` + `   Z` (UTC) + `HHMMSS`."""
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y%m%d") + "   Z" + moment.strftime("%H%M%S")


def parse_date(value: str) -> datetime | None:
    """A SIP2 date, or None when it is blank or not a date. Accepts the 18-character form, a bare date, and ISO."""
    text = value.strip()
    if not text:
        return None
    digits = text.replace("Z", " ")
    try:
        if len(text) >= 18 and text[:8].isdigit():
            return datetime.strptime(text[:8] + text[12:18], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
        if "T" in digits or "-" in digits:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
    return None


def _clean(value: str) -> str:
    if "|" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Sip2Error("a field value cannot contain | or control characters")
    if not value.isascii():
        raise Sip2Error("a field value must be plain ASCII")  # SIP2 text is one byte per character
    return value


def encode(
    code: str,
    fixed: str = "",
    fields: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    *,
    seq: int | None = None,
) -> bytes:
    """One message as bytes, ready to send. With `seq` (0-9) the error-detection trailer is added."""
    want = FIXED_LENGTH.get(code)
    if want is None:
        raise Sip2Error(f"unknown command {code!r}")
    if len(fixed) != want:
        raise Sip2Error(f"command {code} needs {want} fixed characters, got {len(fixed)}")
    if not fixed.isascii() or any(ord(c) < 32 for c in fixed) or "|" in fixed:
        raise Sip2Error("the fixed part has forbidden characters")
    body = code + fixed
    for fid, value in fields:
        if len(fid) != 2 or not fid.isalnum() or not fid.isascii():
            raise Sip2Error(f"bad field id {fid!r}")
        body += fid + _clean(value) + "|"
    if seq is not None:
        if not 0 <= seq <= 9:
            raise Sip2Error("the sequence number is a single digit")
        body += f"AY{seq}AZ"
        body += checksum(body)
    text = body + EOL
    if len(text) > MAX_FRAME:
        raise Sip2Error("the message is too long")
    return text.encode("ascii")


def decode(raw: bytes | str, *, require_checksum: bool = False) -> Message:
    """Read one message. Raises Sip2Error for anything malformed. A present checksum must be right."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME:
            raise Sip2Error("the message is too long")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise Sip2Error("the message is not plain ASCII") from None
    else:
        text = raw
    if len(text) > MAX_FRAME:
        raise Sip2Error("the message is too long")
    text = text.rstrip("\r\n")
    if len(text) < 2 or not text[:2].isdigit():
        raise Sip2Error("the message does not start with a two-digit command")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise Sip2Error("the message contains control characters")
    seq: int | None = None
    protected = False
    # error detection trailer: ...AY<digit>AZ<4 hex> (the checksum covers everything up to and including "AZ")
    tail = text[-9:]
    if len(tail) == 9 and tail[0:2] == "AY" and tail[2].isdigit() and tail[3:5] == "AZ":
        digits = tail[5:]
        if len(digits) != 4 or any(c not in "0123456789ABCDEFabcdef" for c in digits):
            raise Sip2Error("the checksum is not four hex digits")
        if checksum(text[:-4]).upper() != digits.upper():
            raise Sip2Error("the checksum is wrong")
        seq, protected = int(tail[2]), True
        text = text[:-9]
    elif require_checksum:
        raise Sip2Error("the message has no checksum")
    code = text[:2]
    want = FIXED_LENGTH.get(code)
    if want is None:
        raise Sip2Error(f"unknown command {code!r}")
    if len(text) < 2 + want:
        raise Sip2Error(f"command {code} needs {want} fixed characters")
    fixed = text[2 : 2 + want]
    rest = text[2 + want :]
    fields: list[tuple[str, str]] = []
    if rest:
        if not rest.endswith("|"):
            raise Sip2Error("a variable field is not closed with |")
        for part in rest[:-1].split("|"):
            if len(part) < 2 or not part[:2].isalnum():
                raise Sip2Error("a variable field has no two-letter id")
            fields.append((part[:2], part[2:]))
    return Message(code, fixed, tuple(fields), seq, protected)


def flag(value: str) -> bool:
    """Y/1 mean yes in SIP2 fixed fields and some variable fields."""
    return value.strip().upper() in ("Y", "1")
