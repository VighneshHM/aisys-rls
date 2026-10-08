"""Ed25519 signature *verification* in pure Python (RFC 8032 reference algorithm).

Why this exists: the installer must check a package's signature on a machine that has nothing installed yet and no
internet, so it cannot rely on a third-party library. Signing happens once, on the build machine, with a real library
(`cryptography`). Verification is slow (about 10 ms) but is only ever done for a small manifest.
Tested against the RFC 8032 vectors and against signatures made by `cryptography`.
"""

from __future__ import annotations

import hashlib

_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_Q = 2**252 + 27742317777372353535851937790883648493
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = 2 * t1 * t2 * _D % _P
    dd = 2 * z1 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _equal(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(data: bytes) -> tuple[int, int, int, int] | None:
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only if `signature` is a valid Ed25519 signature of `message` by `public_key`. Never raises."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a_point = _decompress(public_key)
        r_bytes = signature[:32]
        r_point = _decompress(r_bytes)
        if a_point is None or r_point is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _Q:
            return False
        h = int.from_bytes(hashlib.sha512(r_bytes + public_key + message).digest(), "little") % _Q
        return _equal(_mul(s, _G), _add(r_point, _mul(h, a_point)))
    except (ValueError, TypeError, ZeroDivisionError):
        return False
