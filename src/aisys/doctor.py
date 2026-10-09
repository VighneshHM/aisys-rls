"""`aisys doctor`: check that this computer and this configuration can run the application (environment checklist).

Each check says OK, WARN (works, but look at it) or FAIL (will not work). It changes nothing. The same list is the
environment checklist in docs/deployment.md.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

from aisys.backup import BackupError, sqlite_file
from aisys.config import Settings

MIN_PYTHON = (3, 12)
MIN_FREE_MB = 500


def _can_write(folder: Path) -> str:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".aisys-doctor"
        probe.write_text("x")
        probe.unlink()
        return ""
    except OSError as exc:
        return str(exc)


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) != 0  # nothing is listening there yet


def _ilms_check(settings: Settings) -> tuple[str, str, str]:
    """The library system: nothing to check for the mock; for SIP2, connect, log in and ask for its status."""
    c = settings.circulation
    if c.ilms_driver == "mock":
        return "OK", "ILMS", 'mock (a stand-in; set [ilms] driver = "sip2" for a real system)'
    if c.ilms_driver != "sip2":
        return "OK", "ILMS", f"driver {c.ilms_driver}"
    if not c.ilms_endpoint or not c.ilms_port:
        return "FAIL", "ILMS", "driver sip2 needs [ilms] endpoint and port"
    from aisys.circulation.connector import ConnectorError
    from aisys.circulation.sip2.adapter import Sip2Connector

    connector = Sip2Connector(c)
    try:
        status = connector.status()
    except ConnectorError as exc:
        return "FAIL", "ILMS", f"{c.ilms_endpoint}:{c.ilms_port}: {exc}"
    finally:
        connector.close()
    online = status.fixed[:1].upper() == "Y"
    return (
        ("OK" if online else "WARN"),
        "ILMS",
        f"SIP2 at {c.ilms_endpoint}:{c.ilms_port} "
        + ("answered and is online" if online else "answered but reports it is offline"),
    )


def run(settings: Settings) -> list[tuple[str, str, str]]:
    """[(OK | WARN | FAIL, check, detail)]"""
    out: list[tuple[str, str, str]] = []

    def add(status: str, check: str, detail: str) -> None:
        out.append((status, check, detail))

    add("OK", "Operating system", f"{platform.system()} {platform.release()} ({platform.machine()})")
    if sys.version_info[:2] >= MIN_PYTHON:
        add("OK", "Python", platform.python_version())
    else:
        add(
            "FAIL",
            "Python",
            f"{platform.python_version()}: version {'.'.join(map(str, MIN_PYTHON))} or newer is needed",
        )
    add("OK", "SQLite", sqlite3.sqlite_version)
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        add("OK", "Full-text search (FTS5)", "available")
    except sqlite3.OperationalError:
        add("WARN", "Full-text search (FTS5)", "this SQLite has no FTS5: search falls back to plain matching")

    try:
        db_file = sqlite_file(settings.database_url)
    except BackupError:
        add("WARN", "Database", "not a SQLite file: backups from this tool will not work")
        db_file = None
    if db_file is not None:
        problem = _can_write(db_file.parent)
        add("FAIL" if problem else "OK", "Database folder", problem or f"{db_file.parent} is writable")
    problem = _can_write(Path(settings.backup_dir))
    add("FAIL" if problem else "OK", "Backup folder", problem or f"{settings.backup_dir} is writable")
    target = Path(settings.backup_dir) if Path(settings.backup_dir).exists() else Path.cwd()
    free_mb = shutil.disk_usage(target).free // 2**20
    add(
        "OK" if free_mb >= MIN_FREE_MB else "WARN",
        "Free disk space",
        f"{free_mb} MB (updates and backups want at least {MIN_FREE_MB} MB)",
    )

    if _port_free(settings.host if settings.host not in ("0.0.0.0", "") else "127.0.0.1", settings.port):  # noqa: S104
        add("OK", "Port", f"{settings.port} is free")
    else:
        add("WARN", "Port", f"{settings.port} is already in use (is the application already running?)")

    if bool(settings.tls_certfile) != bool(settings.tls_keyfile):
        add("FAIL", "TLS", "set both the certificate and the private key, or neither")
    elif settings.tls_certfile:
        missing = [p for p in (settings.tls_certfile, settings.tls_keyfile) if not Path(p).is_file()]
        add(
            "FAIL" if missing else "OK",
            "TLS",
            f"file not found: {missing[0]}" if missing else "HTTPS is served directly",
        )
    else:
        add(
            "WARN" if settings.environment == "production" else "OK",
            "TLS",
            "plain HTTP: put a TLS proxy in front (docs/deployment.md)",
        )

    if settings.environment == "production":
        long_enough = len(settings.secret_key) >= 32
        add(
            "OK" if long_enough else "FAIL",
            "Secret key",
            "set" if long_enough else "AISYS_SECRET_KEY must be at least 32 characters",
        )
    else:
        add("OK", "Environment", f"{settings.environment} (simulators are available; use production on a live site)")
    add(*_ilms_check(settings))
    home = os.environ.get("AISYS_HOME", "")
    if home:
        add("OK" if (Path(home) / "state.json").is_file() else "WARN", "Installation", home)
    return out


def render(results: list[tuple[str, str, str]]) -> str:
    width = max(len(c) for _, c, _ in results)
    lines = [f"  {s:<4}  {c:<{width}}  {d}" for s, c, d in results]
    bad = sum(1 for s, _, _ in results if s == "FAIL")
    warn = sum(1 for s, _, _ in results if s == "WARN")
    lines.append("")
    lines.append("Ready to run." if not bad else f"{bad} problem(s) to fix before this will run.")
    if warn:
        lines.append(f"{warn} warning(s): it will run, but read them.")
    return "\n".join(lines)
