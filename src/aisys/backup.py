"""SQLite backups via the online backup API (safe while the app is running, WAL-aware)."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    sha256: str
    size: int


def sqlite_file(url: str) -> Path:
    if not url.startswith("sqlite:///") or ":memory:" in url:
        raise BackupError("Automatic backup is only implemented for file-based SQLite databases")
    return Path(url.removeprefix("sqlite:///"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(database_url: str, backup_dir: str | Path, label: str) -> BackupInfo:
    """Copy the live database to `<backup_dir>/<label>-<utc timestamp>.db` and verify the copy."""
    src_path = sqlite_file(database_url)
    if not src_path.is_file():
        raise BackupError(f"Database file not found: {src_path}")
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    target = out_dir / f"{safe_label}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.db"

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupError("Backup failed its integrity check")
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"Backup failed: {exc}") from exc
    finally:
        dst.close()
        src.close()
    return BackupInfo(target, _sha256(target), target.stat().st_size)
