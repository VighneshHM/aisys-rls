"""Configuration: TOML file (no secrets) + environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from aisys.circulation.connector import CirculationConfig
from aisys.gate.config import GateConfig
from aisys.notifications.providers import NotificationConfig
from aisys.rfid.registry import DEFAULT_DEVICES, DeviceConfig

DEFAULT_CONFIG_PATH = Path("config/config.toml")
SAMPLE_CONFIG_PATH = Path("config/config.sample.toml")


@dataclass(frozen=True)
class Settings:
    app_name: str = "AISYS RFID Library"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///data/aisys.db"
    session_ttl_minutes: int = 480
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    log_level: str = "INFO"
    log_format: str = "json"
    backup_dir: str = "data/backups"
    devices: tuple[DeviceConfig, ...] = DEFAULT_DEVICES
    circulation: CirculationConfig = CirculationConfig()
    notifications: NotificationConfig = NotificationConfig()
    gate: GateConfig = GateConfig()
    secret_key: str = field(default="", repr=False)


def _interval(value) -> float:
    """0 turns the loop off; anything else is at least one second (a smaller number would spin the CPU)."""
    seconds = float(value)
    return 0.0 if seconds <= 0 else max(seconds, 1.0)


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings. Precedence: env vars > config file > built-in defaults."""
    cfg_path = Path(path or os.environ.get("AISYS_CONFIG", DEFAULT_CONFIG_PATH))
    raw: dict = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)

    app = raw.get("app", {})
    db = raw.get("database", {})
    sec = raw.get("security", {})
    log = raw.get("logging", {})
    backup = raw.get("backup", {})
    devices = (
        tuple(
            DeviceConfig(id=str(d["id"]), kind=str(d.get("kind", "staff_reader")), driver=str(d.get("driver", "mock")))
            for d in raw.get("devices", [])
        )
        or DEFAULT_DEVICES
    )
    circ, ilms = raw.get("circulation", {}), raw.get("ilms", {})
    dc = CirculationConfig()
    circulation = CirculationConfig(
        fine_limit_cents=int(circ.get("fine_limit_cents", dc.fine_limit_cents)),
        loan_days=int(circ.get("loan_days", dc.loan_days)),
        max_renewals=int(circ.get("max_renewals", dc.max_renewals)),
        max_loans=int(circ.get("max_loans", dc.max_loans)),
        fine_per_day_cents=int(circ.get("fine_per_day_cents", dc.fine_per_day_cents)),
        grace_days=int(circ.get("grace_days", dc.grace_days)),
        max_fine_per_loan_cents=int(circ.get("max_fine_per_loan_cents", dc.max_fine_per_loan_cents)),
        ilms_driver=str(ilms.get("driver", dc.ilms_driver)),
        ilms_protocol=str(ilms.get("protocol", dc.ilms_protocol)),
        ilms_retries=int(ilms.get("retries", dc.ilms_retries)),
        ilms_backoff_s=float(ilms.get("backoff_s", dc.ilms_backoff_s)),
    )
    notif, gate_raw = raw.get("notifications", {}), raw.get("gate", {})
    dn, dg = NotificationConfig(), GateConfig()
    notifications = NotificationConfig(
        email_driver=str(notif.get("email", dn.email_driver)),
        sms_driver=str(notif.get("sms", dn.sms_driver)),
        print_driver=str(notif.get("print", dn.print_driver)),
        max_attempts=int(notif.get("max_attempts", dn.max_attempts)),
        backoff_s=float(notif.get("backoff_s", dn.backoff_s)),
        reminders=bool(notif.get("reminders", dn.reminders)),
        remind_days_before=min(max(int(notif.get("remind_days_before", dn.remind_days_before)), 1), 30),
        auto_dispatch_s=_interval(notif.get("auto_dispatch_seconds", dn.auto_dispatch_s)),
    )
    gate = GateConfig(duplicate_window_s=int(gate_raw.get("duplicate_window_s", dg.duplicate_window_s)))
    d = Settings()

    settings = Settings(
        app_name=app.get("name", d.app_name),
        environment=os.environ.get("AISYS_ENV", app.get("environment", d.environment)),
        host=os.environ.get("AISYS_HOST", app.get("host", d.host)),
        port=int(os.environ.get("AISYS_PORT", app.get("port", d.port))),
        database_url=os.environ.get("AISYS_DATABASE_URL", db.get("url", d.database_url)),
        session_ttl_minutes=int(sec.get("session_ttl_minutes", d.session_ttl_minutes)),
        max_failed_logins=int(sec.get("max_failed_logins", d.max_failed_logins)),
        lockout_minutes=int(sec.get("lockout_minutes", d.lockout_minutes)),
        log_level=log.get("level", d.log_level),
        log_format=log.get("format", d.log_format),
        backup_dir=os.environ.get("AISYS_BACKUP_DIR", backup.get("dir", d.backup_dir)),
        devices=devices,
        circulation=circulation,
        notifications=notifications,
        gate=gate,
        secret_key=os.environ.get("AISYS_SECRET_KEY", ""),
    )
    if settings.environment == "production" and len(settings.secret_key) < 32:
        raise RuntimeError("AISYS_SECRET_KEY (>= 32 chars) is required in production")
    return settings
