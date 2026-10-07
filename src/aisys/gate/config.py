"""Gate settings (config file section [gate])."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateConfig:
    duplicate_window_s: int = 60  # the same tag alarming again at the same gate within this time is a duplicate
