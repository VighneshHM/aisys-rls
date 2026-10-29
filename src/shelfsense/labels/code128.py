"""Code 128 barcodes (subset B: printable ASCII), returned as bar and space widths that a page draws as an SVG.

The widths are the standard patterns from ISO/IEC 15417. `encode` returns a string of digits: bar, space, bar, space...
each digit the width of that bar or space in modules, ending with the stop pattern. The checksum is added here, and a
decoder is included so the tests can prove that what is drawn reads back as what was asked for.
"""

from __future__ import annotations

PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 "
    "123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 "
    "232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 113321 133121 "
    "313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 "
    "111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 "
    "114311 411113 411311 113141 114131 311141 411131 211412 211214 211232"
).split()
START_B = 104
STOP = "2331112"
MAX_LENGTH = 40


def encodable(text: str) -> bool:
    return bool(text) and len(text) <= MAX_LENGTH and all(32 <= ord(c) <= 126 for c in text)


def values(text: str) -> list[int]:
    """The symbol values: start B, one per character, the checksum."""
    if not encodable(text):
        raise ValueError("A barcode holds 1 to 40 printable ASCII characters")
    body = [ord(c) - 32 for c in text]
    checksum = (START_B + sum(v * (i + 1) for i, v in enumerate(body))) % 103
    return [START_B, *body, checksum]


def encode(text: str) -> str:
    """Bar/space widths for the whole symbol including the stop pattern, for example '211214...'."""
    return "".join(PATTERNS[v] for v in values(text)) + STOP


def decode(widths: str) -> str:
    """Read widths back to text; raises ValueError if the pattern or its checksum is wrong. Used by tests."""
    if not widths.endswith(STOP):
        raise ValueError("no stop pattern")
    body = widths[: -len(STOP)]
    if len(body) % 6:
        raise ValueError("bad length")
    symbols = [PATTERNS.index(body[i : i + 6]) for i in range(0, len(body), 6)]
    if symbols[0] != START_B:
        raise ValueError("not subset B")
    *data, check = symbols[1:]
    if (START_B + sum(v * (i + 1) for i, v in enumerate(data))) % 103 != check:
        raise ValueError("bad checksum")
    return "".join(chr(v + 32) for v in data)
