"""Configuration: TOML file (no secrets) + environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
    secret_key: str = field(default="", repr=False)


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
    d = Settings()

    settings = Settings(
        app_name=app.get("name", d.app_name),
        environment=os.environ.get("AISYS_ENV", app.get("environment", d.environment)),
        host=app.get("host", d.host),
        port=int(app.get("port", d.port)),
        database_url=os.environ.get("AISYS_DATABASE_URL", db.get("url", d.database_url)),
        session_ttl_minutes=int(sec.get("session_ttl_minutes", d.session_ttl_minutes)),
        max_failed_logins=int(sec.get("max_failed_logins", d.max_failed_logins)),
        lockout_minutes=int(sec.get("lockout_minutes", d.lockout_minutes)),
        log_level=log.get("level", d.log_level),
        log_format=log.get("format", d.log_format),
        secret_key=os.environ.get("AISYS_SECRET_KEY", ""),
    )
    if settings.environment == "production" and len(settings.secret_key) < 32:
        raise RuntimeError("AISYS_SECRET_KEY (>= 32 chars) is required in production")
    return settings
