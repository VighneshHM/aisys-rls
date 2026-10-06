"""Device registry: builds adapters from configuration so hardware is swapped without code changes.

To support a real reader: write a class implementing `StaffReader`, then
`DeviceRegistry.register_driver("vendorx", VendorXReader)` and set `driver = "vendorx"` in config.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aisys.rfid.devices import Device, DeviceHealth, StaffReader
from aisys.rfid.mock import MockStaffReader

KINDS = {"staff_reader": StaffReader}


class DeviceConfigError(ValueError):
    """The device configuration is invalid (fails fast at startup)."""


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    kind: str = "staff_reader"
    driver: str = "mock"


DEFAULT_DEVICES = (DeviceConfig("staff-1", "staff_reader", "mock"),)


class DeviceRegistry:
    _drivers: dict[str, Callable[..., Device]] = {"mock": MockStaffReader}

    def __init__(self, configs: tuple[DeviceConfig, ...] = DEFAULT_DEVICES) -> None:
        self._devices: dict[str, Device] = {}
        for cfg in configs:
            if cfg.id in self._devices:
                raise DeviceConfigError(f"duplicate device id: {cfg.id}")
            if cfg.kind not in KINDS:
                raise DeviceConfigError(f"device {cfg.id}: unknown kind '{cfg.kind}' (known: {sorted(KINDS)})")
            factory = self._drivers.get(cfg.driver)
            if factory is None:
                raise DeviceConfigError(
                    f"device {cfg.id}: unknown driver '{cfg.driver}' (known: {sorted(self._drivers)})"
                )
            device = factory(device_id=cfg.id)
            if not isinstance(device, KINDS[cfg.kind]):
                raise DeviceConfigError(f"device {cfg.id}: driver '{cfg.driver}' does not implement {cfg.kind}")
            self._devices[cfg.id] = device

    @classmethod
    def register_driver(cls, name: str, factory: Callable[..., Device]) -> None:
        cls._drivers[name] = factory

    def get(self, device_id: str, kind: str = "staff_reader") -> Device | None:
        device = self._devices.get(device_id)
        return device if device is not None and device.kind == kind else None

    def all(self) -> list[Device]:
        return list(self._devices.values())

    def describe(self) -> list[dict]:
        out = []
        for d in self._devices.values():
            h: DeviceHealth = d.health()
            out.append({"id": d.device_id, "kind": d.kind, "driver": type(d).__name__,
                        "mock": isinstance(d, MockStaffReader), "online": h.online, "detail": h.detail})  # fmt: skip
        return out
