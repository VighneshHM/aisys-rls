"""Gate settings (config file section [gate])."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateConfig:
    duplicate_window_s: int = 60  # the same tag alarming again at the same gate within this time is a duplicate
    poll_seconds: float = 0  # read every real gate this often while the application runs; 0 = off (see gate.service)
