"""FR03: the SIP2 message codec - building, reading, the checksum, and refusing anything malformed."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from aisys.circulation.sip2 import codec
from aisys.circulation.sip2.codec import MAX_FRAME, Message, Sip2Error, decode, encode

pytestmark = pytest.mark.req("FR03")

STAMP = "20261009   Z101500"


def sample(code: str) -> bytes:
    """A valid message of each command, with a fixed part of the right length."""
    fixed = "x" * codec.FIXED_LENGTH[code]
    return encode(code, fixed, [("AA", "M0001"), ("AB", "10042")], seq=3)


# ------------------------------------------------------------------ the checksum
def test_the_checksum_added_to_the_character_sum_is_zero_modulo_65536():
    """The SIP2 rule: sum the characters up to and including AZ, invert the low 16 bits and add one."""
    wire = encode("93", "00", [("CN", "desk"), ("CO", "secret")], seq=1).decode("ascii").rstrip(chr(13))
    before, digits = wire[:-4], wire[-4:]
    assert before.endswith("AY1AZ")
    assert (sum(before.encode("ascii")) + int(digits, 16)) % 65536 == 0


def test_known_checksum_value():
    # worked by hand: the characters of "98AY1AZ" add up to 471 (0x1D7); 0x10000 - 0x1D7 = 0xFE29
    assert sum(b"98AY1AZ") == 0x1D7 and codec.checksum("98AY1AZ") == "FE29"


def test_the_checksum_is_always_four_upper_case_hex_digits():
    for text in ("", "A", "x" * 5000, "9300CNdesk|AY1AZ"):
        digits = codec.checksum(text)
        assert len(digits) == 4 and digits == digits.upper() and int(digits, 16) < 65536


def test_a_message_without_error_detection_has_no_trailer():
    wire = encode("93", "00", [("CN", "desk")]).decode()
    assert wire == "9300CNdesk|\r" and "AY" not in wire
    msg = decode(wire)
    assert (msg.protected, msg.seq) == (False, None)


@pytest.mark.parametrize("seq", range(10))
def test_every_sequence_digit_round_trips(seq):
    msg = decode(encode("99", "0080" + "2.00", [], seq=seq))
    assert msg.seq == seq and msg.protected


# ------------------------------------------------------------------ round trips
@pytest.mark.parametrize("code", sorted(codec.FIXED_LENGTH))
def test_every_command_round_trips(code):
    msg = decode(sample(code))
    assert msg.code == code and msg.fixed == "x" * codec.FIXED_LENGTH[code]
    assert msg.get("AA") == "M0001" and msg.get("AB") == "10042" and msg.get("ZZ", "none") == "none"


def test_repeated_fields_keep_their_order():
    wire = encode("64", " " * 59, [("AU", "A1"), ("AU", "A2"), ("AU", "A3"), ("AE", "Priya")])
    msg = decode(wire)
    assert msg.all("AU") == ["A1", "A2", "A3"] and msg.get("AE") == "Priya"


def test_an_empty_field_value_is_kept():
    msg = decode(encode("23", "001" + STAMP, [("AO", ""), ("AA", "M1"), ("AC", "")]))
    assert msg.fields == (("AO", ""), ("AA", "M1"), ("AC", ""))


def test_lower_case_hex_in_the_checksum_is_accepted():
    wire = encode("99", "00802.00", [], seq=2).decode().rstrip("\r")
    assert decode(wire[:-4] + wire[-4:].lower()).seq == 2


def test_the_message_object_is_immutable_and_comparable():
    m = Message("94", "1")
    with pytest.raises(AttributeError):
        m.code = "x"  # type: ignore[misc]
    assert m == Message("94", "1")


# ------------------------------------------------------------------ refusing bad messages when reading
BAD = {
    "wrong checksum": lambda: sample("11")[:-2] + b"0\r",
    "checksum not hex": lambda: b"99008002.00AY1AZ12G4\r",
    "unknown command": lambda: b"00AAx|\r",
    "no command": lambda: b"AAx|\r",
    "empty": lambda: b"",
    "one character": lambda: b"9",
    "fixed part too short": lambda: b"11NN\r",
    "field not closed": lambda: b"9300CNdesk\r",
    "field without id": lambda: b"9300|\r",
    "field one letter": lambda: b"9300A|\r",
    "control character": lambda: b"9300CNde\x01sk|\r",
    "escape": lambda: b"9300CNde\x1bsk|\r",
    "delete character": lambda: b"9300CNde\x7fsk|\r",
    "not ascii": lambda: "9300CNdésk|\r".encode(),
    "too long": lambda: b"9300CN" + b"x" * MAX_FRAME + b"|\r",
    "high bytes": lambda: b"93\xff\xfe\r",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_malformed_message_is_refused_with_sip2_error(name):
    with pytest.raises(Sip2Error):
        decode(BAD[name]())


def test_a_changed_character_is_caught_by_the_checksum():
    wire = bytearray(sample("12"))
    wire[5] ^= 0x01
    with pytest.raises(Sip2Error, match="checksum"):
        decode(bytes(wire))


def test_a_missing_checksum_is_refused_when_one_is_required():
    with pytest.raises(Sip2Error, match="no checksum"):
        decode(encode("99", "00802.00", []), require_checksum=True)


def test_a_line_feed_after_the_carriage_return_is_tolerated():
    assert decode(b"9300CNdesk|\r\n").get("CN") == "desk"


# ------------------------------------------------------------------ refusing bad input when building
def test_encode_refuses_what_could_break_the_message():
    with pytest.raises(Sip2Error, match=r"\|"):
        encode("23", "001" + STAMP, [("AA", "M1|AB")])  # a value that would smuggle in another field
    with pytest.raises(Sip2Error):
        encode("23", "001" + STAMP, [("AA", "M1\rAA")])
    with pytest.raises(Sip2Error):
        encode("23", "001" + STAMP, [("AA", "M1\n")])
    with pytest.raises(Sip2Error, match="ASCII"):
        encode("23", "001" + STAMP, [("AA", "Mé")])
    with pytest.raises(Sip2Error, match="field id"):
        encode("23", "001" + STAMP, [("A", "x")])
    with pytest.raises(Sip2Error, match="field id"):
        encode("23", "001" + STAMP, [("A|", "x")])
    with pytest.raises(Sip2Error, match="fixed"):
        encode("23", "short", [])
    with pytest.raises(Sip2Error, match="unknown"):
        encode("55", "", [])
    with pytest.raises(Sip2Error, match="digit"):
        encode("99", "00802.00", [], seq=10)
    with pytest.raises(Sip2Error, match="too long"):
        encode("23", "001" + STAMP, [("AA", "x" * MAX_FRAME)])
    with pytest.raises(Sip2Error, match="forbidden"):
        encode("23", "001\x01" + STAMP[:-1], [])


# ------------------------------------------------------------------ dates
def test_the_transaction_stamp_is_eighteen_characters_in_utc():
    s = codec.stamp(datetime(2026, 10, 9, 10, 15, 0, tzinfo=UTC))
    assert s == STAMP and len(s) == codec.DATE_WIDTH


@pytest.mark.parametrize(
    "text, expected",
    [
        ("20261009    101500", datetime(2026, 10, 9, 10, 15, tzinfo=UTC)),
        ("20261009   Z101500", datetime(2026, 10, 9, 10, 15, tzinfo=UTC)),
        ("20261009", datetime(2026, 10, 9, tzinfo=UTC)),
        ("2026-10-09T10:15:00Z", datetime(2026, 10, 9, 10, 15, tzinfo=UTC)),
        ("2026-10-09", datetime(2026, 10, 9, tzinfo=UTC)),
        ("", None),
        ("   ", None),
        ("not a date", None),
        ("20261399    101500", None),
        ("99999999", None),
    ],
)
def test_dates_in_the_forms_servers_send(text, expected):
    assert codec.parse_date(text) == expected


def test_yes_and_no_flags():
    assert codec.flag("Y") and codec.flag("1") and codec.flag(" y ")
    assert not codec.flag("N") and not codec.flag("") and not codec.flag("0")


# ------------------------------------------------------------------ nothing but Sip2Error ever comes out
def test_random_and_damaged_input_only_ever_raises_sip2_error():
    rng = random.Random(20261009)  # noqa: S311  (a repeatable test input, not security)
    seeds = [sample(c) for c in codec.FIXED_LENGTH]
    for _ in range(3000):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 6)):
            roll = rng.random()
            if roll < 0.4 and data:
                data[rng.randrange(len(data))] = rng.randrange(256)
            elif roll < 0.7 and data:
                del data[rng.randrange(len(data))]
            else:
                data.insert(rng.randrange(len(data) + 1), rng.randrange(256))
        try:
            decode(bytes(data))
        except Sip2Error:
            pass


def test_random_bytes_only_ever_raise_sip2_error():
    rng = random.Random(7)  # noqa: S311
    for _ in range(2000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 80)))
        try:
            decode(blob)
        except Sip2Error:
            pass
