"""Mock staff reader: an in-memory pad of tags. Stands in for vendor hardware that is out of scope (SOP section 1).

It behaves like a real encoder (tags keep their written identifier and security bit) and supports failure injection so
tests and demos can exercise offline / write-failure paths.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from aisys.rfid.devices import DeviceError, DeviceHealth, DeviceUnavailable, Snapshot
from aisys.rfid.events import Passage, TagObserved, is_valid_tag_uid, normalize_tag_uid


@dataclass
class _TagMemory:
    item_ref: str | None = None
    armed: bool = False


class MockStaffReader:
    kind = "staff_reader"
    is_mock = True

    def __init__(self, device_id: str = "staff-1", **_options: object) -> None:
        self.device_id = device_id
        self.online = True
        self.indications: list[str] = []  # the beeps/LED flashes a person would have heard/seen
        self._pad: dict[str, _TagMemory] = {}
        self._fail_next: set[str] = set()
        self._lock = RLock()

    # ---- simulator controls (not part of the device interface) -------------------------------------
    def place_tag(self, uid: str | None = None, *, replace: bool = False) -> str:
        """Put a tag on the pad. Without a UID a fresh blank 96-bit EPC-style tag is generated.

        `replace=True` models holding a different tag up to the reader: any tag already on the pad is removed first.
        The default adds to the pad, which a real reader field with several tags (bulk reading) also allows.
        """
        uid = normalize_tag_uid(uid) if uid else "E280" + secrets.token_hex(10).upper()
        if not is_valid_tag_uid(uid):
            raise ValueError("tag UID must be 8-32 hex characters")
        with self._lock:
            if replace:
                self._pad.clear()
            self._pad.setdefault(uid, _TagMemory())
        return uid

    def remove_tag(self, uid: str) -> None:
        with self._lock:
            self._pad.pop(normalize_tag_uid(uid), None)

    def clear_pad(self) -> None:
        with self._lock:
            self._pad.clear()

    def fail_next(self, command: str) -> None:
        """Make the next `encode` / `set_security` / `clear` call raise DeviceError (failure injection)."""
        self._fail_next.add(command)

    def memory_of(self, uid: str) -> _TagMemory | None:
        return self._pad.get(normalize_tag_uid(uid))

    # ---- StaffReader interface ----------------------------------------------------------------------
    def health(self) -> DeviceHealth:
        return DeviceHealth(self.online, "mock reader" if self.online else "mock reader switched off")

    def _require_online(self) -> None:
        if not self.online:
            raise DeviceUnavailable(f"{self.device_id} is offline")

    def _maybe_fail(self, command: str) -> None:
        if command in self._fail_next:
            self._fail_next.discard(command)
            raise DeviceError(f"{self.device_id}: simulated {command} failure")

    def _tag(self, uid: str) -> _TagMemory:
        tag = self._pad.get(normalize_tag_uid(uid))
        if tag is None:
            raise DeviceError(f"{self.device_id}: tag {uid} is not in the reader field")
        return tag

    def read_field(self) -> list[TagObserved]:
        self._require_online()
        with self._lock:
            return [
                TagObserved(uid, self.device_id, TagObserved.now(), mem.item_ref, mem.armed)
                for uid, mem in self._pad.items()
            ]

    def encode(self, uid: str, item_ref: str) -> None:
        self._require_online()
        self._maybe_fail("encode")
        with self._lock:
            tag = self._tag(uid)
            tag.item_ref, tag.armed = item_ref, True

    def set_security(self, uid: str, armed: bool) -> None:
        self._require_online()
        self._maybe_fail("set_security")
        with self._lock:
            self._tag(uid).armed = armed

    def clear(self, uid: str) -> None:
        self._require_online()
        self._maybe_fail("clear")
        with self._lock:
            tag = self._tag(uid)
            tag.item_ref, tag.armed = None, False

    def indicate(self, pattern: str) -> None:
        self.indications.append(pattern)


class MockHandheld:
    """Mock handheld reader: whatever the simulator loads is "in range" and is returned by every sweep.

    Like real UHF hardware it may return the same tag more than once in a sweep; the application must cope.
    """

    kind = "handheld"
    is_mock = True

    def __init__(self, device_id: str = "handheld-1", **_options: object) -> None:
        self.device_id = device_id
        self.online = True
        self.indications: list[str] = []
        self._in_range: list[str] = []
        self._fail_next: set[str] = set()
        self._lock = RLock()

    # ---- simulator controls ---------------------------------------------------------------------
    def load(self, uids: list[str]) -> None:
        """Replace what is in range (repeat a UID to simulate a duplicate read)."""
        with self._lock:
            self._in_range = [normalize_tag_uid(u) for u in uids]

    def clear(self) -> None:
        with self._lock:
            self._in_range = []

    def fail_next(self, command: str = "scan") -> None:
        self._fail_next.add(command)

    # ---- Handheld interface ---------------------------------------------------------------------
    def health(self) -> DeviceHealth:
        return DeviceHealth(self.online, "mock handheld" if self.online else "mock handheld switched off")

    def scan(self) -> list[TagObserved]:
        if not self.online:
            raise DeviceUnavailable(f"{self.device_id} is offline")
        if "scan" in self._fail_next:
            self._fail_next.discard("scan")
            raise DeviceError(f"{self.device_id}: simulated scan failure")
        with self._lock:
            return [TagObserved(uid, self.device_id, TagObserved.now()) for uid in self._in_range]

    def indicate(self, pattern: str) -> None:
        self.indications.append(pattern)


class MockGate:
    """Mock security gate. The simulator injects passages; `poll` hands them to the application once."""

    kind = "gate"
    is_mock = True

    def __init__(self, device_id: str = "gate-1", **_options: object) -> None:
        self.device_id = device_id
        self.online = True
        self.indications: list[str] = []
        self._waiting: list[Passage] = []
        self._fail_next: set[str] = set()
        self._lock = RLock()

    # ---- simulator controls ---------------------------------------------------------------------
    def walk_through(self, tags: list[tuple[str, bool | None]], people: int = 1) -> None:
        """Someone walks through carrying `tags` = (uid, security bit as read from the tag)."""
        now = TagObserved.now()
        seen = tuple(TagObserved(normalize_tag_uid(u), self.device_id, now, None, armed) for u, armed in tags)
        with self._lock:
            self._waiting.append(Passage(self.device_id, TagObserved.now(), max(1, people), seen))

    def fail_next(self, command: str = "poll") -> None:
        self._fail_next.add(command)

    # ---- Gate interface -------------------------------------------------------------------------
    def health(self) -> DeviceHealth:
        return DeviceHealth(self.online, "mock gate" if self.online else "mock gate switched off")

    def poll(self) -> list[Passage]:
        if not self.online:
            raise DeviceUnavailable(f"{self.device_id} is offline")
        if "poll" in self._fail_next:
            self._fail_next.discard("poll")
            raise DeviceError(f"{self.device_id}: simulated poll failure")
        with self._lock:
            out, self._waiting = self._waiting, []
            return out

    def indicate(self, pattern: str) -> None:
        self.indications.append(pattern)


class MockCamera:
    """Mock CCTV camera: draws a labelled placeholder frame (an SVG) instead of a real photograph."""

    kind = "camera"
    is_mock = True

    def __init__(self, device_id: str = "camera-1", **_options: object) -> None:
        self.device_id = device_id
        self.online = True
        self._fail_next: set[str] = set()

    def fail_next(self, command: str = "capture") -> None:
        self._fail_next.add(command)

    def health(self) -> DeviceHealth:
        return DeviceHealth(self.online, "mock camera" if self.online else "mock camera switched off")

    def capture(self, caption: str) -> Snapshot:
        if not self.online:
            raise DeviceUnavailable(f"{self.device_id} is offline")
        if "capture" in self._fail_next:
            self._fail_next.discard("capture")
            raise DeviceError(f"{self.device_id}: simulated capture failure")
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [html.escape(part) for part in caption.splitlines()[:4]]
        text = "".join(
            f'<text x="24" y="{236 + 26 * n}" fill="#e6edf7" font-size="18" font-family="monospace">{line}</text>'
            for n, line in enumerate(lines)
        )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="640" height="360">'
            '<rect width="640" height="360" fill="#0b1220"/>'
            '<rect x="0" y="0" width="640" height="360" fill="none" stroke="#2b3a55" stroke-width="4"/>'
            '<rect x="40" y="70" width="560" height="120" rx="6" fill="#111c33" stroke="#2b3a55"/>'
            '<circle cx="320" cy="108" r="22" fill="#2b3a55"/>'
            '<rect x="288" y="136" width="64" height="48" rx="12" fill="#2b3a55"/>'
            '<circle cx="28" cy="28" r="7" fill="#ef4444"/>'
            '<text x="44" y="34" fill="#ef4444" font-size="18" font-family="monospace">REC</text>'
            '<text x="500" y="34" fill="#9fb0c9" font-size="14" font-family="monospace">MOCK CCTV</text>'
            f'<text x="24" y="216" fill="#9fb0c9" font-size="14" font-family="monospace">'
            f"{html.escape(self.device_id)} - {stamp}</text>"
            f"{text}</svg>"
        )
        return Snapshot("image/svg+xml", svg.encode("utf-8"))
