"""Mock staff reader: an in-memory pad of tags. Stands in for vendor hardware that is out of scope (SOP section 1).

It behaves like a real encoder (tags keep their written identifier and security bit) and supports failure injection so
tests and demos can exercise offline / write-failure paths.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from threading import RLock

from aisys.rfid.devices import DeviceError, DeviceHealth, DeviceUnavailable
from aisys.rfid.events import TagObserved, is_valid_tag_uid, normalize_tag_uid


@dataclass
class _TagMemory:
    item_ref: str | None = None
    armed: bool = False


class MockStaffReader:
    kind = "staff_reader"

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
