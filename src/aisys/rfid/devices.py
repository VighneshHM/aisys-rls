"""Device-neutral interfaces (SOP section 6, RFID middleware layer).

Commands go *to* a device (`encode`, `set_security`, `indicate`); events come *from* it (`TagObserved`).
Handheld readers, gates, cameras and printers get their own interfaces in later stages and share `Device`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aisys.rfid.events import TagObserved


class DeviceError(Exception):
    """A device refused or failed a command."""


class DeviceUnavailable(DeviceError):  # noqa: N818 - reads better than DeviceUnavailableError at call sites
    """The device is offline / unreachable."""


@dataclass(frozen=True)
class DeviceHealth:
    online: bool
    detail: str = ""


@runtime_checkable
class Device(Protocol):
    device_id: str
    kind: str

    def health(self) -> DeviceHealth: ...


@runtime_checkable
class Handheld(Device, Protocol):
    """Handheld reader for walking a shelf: one sweep returns every tag in range (the same tag may repeat)."""

    def scan(self) -> list[TagObserved]:
        """One sweep. Raises DeviceUnavailable if the handheld is off or out of reach."""

    def indicate(self, pattern: str) -> None:
        """Audible/visible confirmation: 'ok' (one beep, green), 'warning' (two beeps, amber), 'error' (long, red)."""


@runtime_checkable
class StaffReader(Device, Protocol):
    """Staff-station reader/encoder: reads tags on the pad and writes the item identifier + security bit."""

    def read_field(self) -> list[TagObserved]:
        """Tags currently in the reader field."""

    def encode(self, uid: str, item_ref: str) -> None:
        """Write the item identifier (accession number) to the tag."""

    def set_security(self, uid: str, armed: bool) -> None:
        """Arm/disarm the anti-theft (EAS/AFI) bit."""

    def clear(self, uid: str) -> None:
        """Undo `encode`: wipe the identifier and disarm. Used to roll back a failed association."""

    def indicate(self, pattern: str) -> None:
        """Audible/visible confirmation: 'ok', 'warning' or 'error'."""
