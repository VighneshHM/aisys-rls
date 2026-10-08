"""SQLite backups: create, verify, compare, restore. All of it works on whole-file copies made with the online
backup API (safe while the app is running, WAL-aware).

Proof that data is intact is a *fingerprint*: for every table the row count and a SHA-256 over its rows in a fixed
order. Comparing two fingerprints says, table by table, whether anything differs. The sessions and audit
tables grow just by using the app, so they are reported as "activity" and never counted as damage.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic.script import ScriptDirectory

from aisys import __version__
from aisys.migrate import MIGRATIONS_DIR

VOLATILE_TABLES = frozenset({"sessions", "audit_events"})  # change with ordinary use, so not "damage"
# the import's own log: a failed import is (rightly) recorded as failed, which is not a change to library data
BOOKKEEPING_TABLES = frozenset({"migration_batches", "migration_rows"})
MANIFEST_SUFFIX = ".json"
BACKUP_WAIT_S = 30.0  # how long making a backup waits for a busy database
RESTORE_WAIT_S = 20.0  # how long a restore waits for the app to let go of the database
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.db$")


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


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _integrity_ok(path: Path) -> bool:
    try:
        con = _readonly(path)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == "ok"


# ------------------------------------------------------------------ fingerprints and comparison
def _row_bytes(row: tuple) -> bytes:
    cells = [v.hex() if isinstance(v, bytes) else v for v in row]  # blobs (CCTV snapshots) are hashed as hex
    return (json.dumps(cells, ensure_ascii=True, separators=(",", ":"), default=str) + "\n").encode()


def fingerprint(path: Path) -> dict[str, dict]:
    """{table: {rows, sha256}} for every table, read in one consistent snapshot."""
    con = sqlite3.connect(Path(path))
    out: dict[str, dict] = {}
    try:
        con.execute("BEGIN")
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE 'item_fts%' ORDER BY name"  # the search index is derived data, not records
            )
        ]
        for name in names:
            h, n = hashlib.sha256(), 0
            quoted = '"' + name.replace('"', '""') + '"'  # the name comes from the schema itself
            for row in con.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):  # noqa: S608
                h.update(_row_bytes(row))
                n += 1
            out[name] = {"rows": n, "sha256": h.hexdigest()}
    finally:
        con.close()
    return out


def data_digest(fp: dict[str, dict]) -> str:
    """One checksum for all the data that is not mere activity."""
    h = hashlib.sha256()
    for name in sorted(fp):
        if name not in VOLATILE_TABLES:
            h.update(f"{name}:{fp[name]['rows']}:{fp[name]['sha256']}\n".encode())
    return h.hexdigest()


def compare(live: Path, backup: Path) -> dict:
    """Table-by-table comparison of the live database with a backup."""
    a, b = fingerprint(live), fingerprint(backup)
    tables = []
    for name in sorted(set(a) | set(b)):
        la, lb = a.get(name), b.get(name)
        match = la == lb
        status = "same" if match else "only_in_backup" if la is None else "only_in_live" if lb is None else "changed"
        tables.append(
            {
                "table": name,
                "live_rows": la["rows"] if la else None,
                "backup_rows": lb["rows"] if lb else None,
                "match": match,
                "status": status,
                "activity": name in VOLATILE_TABLES,
            }
        )
    differing = [t["table"] for t in tables if not t["match"] and not t["activity"]]
    return {
        "tables": tables,
        "data_intact": not differing,
        "library_data_intact": all(t in BOOKKEEPING_TABLES for t in differing),  # ignoring only the import log
        "differing": differing,
        "activity_since": [t["table"] for t in tables if not t["match"] and t["activity"]],
        "live_digest": data_digest(a),
        "backup_digest": data_digest(b),
    }


# ------------------------------------------------------------------ revisions
def known_revisions() -> list[str]:
    """Every schema revision this version of the app knows, oldest first."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def head() -> str:
    return known_revisions()[-1]


def _revision_of(path: Path) -> str | None:
    try:
        con = _readonly(path)
        try:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


# ------------------------------------------------------------------ create and list
def _manifest_path(db_file: Path) -> Path:
    return db_file.with_name(db_file.name + MANIFEST_SUFFIX)


def _read_manifest(db_file: Path) -> dict | None:
    """The manifest next to a backup, with every field forced to a safe type. A missing, unreadable or nonsensical
    manifest is treated as "no manifest" (or an unmatched checksum), never as an error that breaks the list."""
    try:
        raw = json.loads(_manifest_path(db_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    def text(key: str, limit: int = 200) -> str:
        value = raw.get(key)
        return value[:limit] if isinstance(value, str) else ""

    tables = raw.get("tables")
    tables = (
        {k: v for k, v in tables.items() if isinstance(k, str) and isinstance(v, int)}
        if isinstance(tables, dict)
        else {}
    )
    return {
        "label": text("label", 60),
        "note": text("note"),
        "created_at": text("created_at", 40),
        "revision": text("revision", 20) or None,
        "app_version": text("app_version", 20),
        "sha256": text("sha256", 64),
        "tables": tables,
    }


def create_backup(database_url: str, backup_dir: str | Path, label: str, note: str = "") -> BackupInfo:
    """Copy the live database to `<backup_dir>/<label>-<utc timestamp>.db`, verify the copy, and write its manifest."""
    src_path = sqlite_file(database_url)
    if not src_path.is_file():
        raise BackupError(f"Database file not found: {src_path}")
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in label) or "backup"
    target = out_dir / f"{safe_label}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.db"

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(target)
    try:
        _copy_pages(src, dst, BACKUP_WAIT_S)
        dst.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, no -wal / -shm beside it
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupError("Backup failed its integrity check")
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"Backup failed: {exc}") from exc
    except BackupError:
        target.unlink(missing_ok=True)
        raise
    finally:
        dst.close()
        src.close()
    info = BackupInfo(target, _sha256(target), target.stat().st_size)
    fp = fingerprint(target)
    manifest = {
        "name": target.name,
        "label": safe_label,
        "note": note,
        "created_at": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "revision": _revision_of(target),
        "size": info.size,
        "sha256": info.sha256,
        "tables": {t: v["rows"] for t, v in fp.items()},
        "data_digest": data_digest(fp),
    }
    _manifest_path(target).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return info


def usable_name(name: str) -> bool:
    """Backup file names are plain (letters, digits, dot, dash, underscore); others are never offered or accepted."""
    return bool(NAME_RE.match(name))


def skipped_files(backup_dir: str | Path) -> list[str]:
    """`.db` files in the folder that are ignored because their names are not plain (so the user can rename them)."""
    folder = Path(backup_dir)
    return sorted(p.name for p in folder.glob("*.db") if not usable_name(p.name)) if folder.is_dir() else []


def list_backups(backup_dir: str | Path) -> list[dict]:
    """The backups in the folder, newest first. A copy made by hand (no manifest) is listed too."""
    folder = Path(backup_dir)
    if not folder.is_dir():
        return []
    out = []
    files = [p for p in folder.glob("*.db") if usable_name(p.name)]
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        m = _read_manifest(path) or {}
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "label": m.get("label") or path.name.rsplit("-", 1)[0],
                "note": m.get("note", ""),
                "created_at": m.get("created_at") or datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "size": stat.st_size,
                "revision": m.get("revision"),
                "has_manifest": bool(m),
                "app_version": m.get("app_version", ""),
                "rows": sum(m.get("tables", {}).values()) if m else None,
            }
        )
    return out


def verify_backup(path: Path) -> dict:
    """Is this file a healthy backup this version of the app can restore?"""
    path = Path(path)
    problems: list[str] = []
    if not path.is_file():
        return {"name": path.name, "ok": False, "problems": ["The file does not exist"]}
    manifest = _read_manifest(path)
    sha = _sha256(path)
    sha_ok = None if manifest is None else manifest.get("sha256") == sha
    if sha_ok is False:
        problems.append("The file has changed since it was made (its checksum does not match its manifest)")
    integrity = _integrity_ok(path)
    if not integrity:
        problems.append("The database file is damaged (it fails SQLite's integrity check)")
    revision = _revision_of(path) if integrity else None
    known = known_revisions()
    if integrity and revision is None:
        problems.append("This is not a backup of this application (no schema version found)")
    elif revision is not None and revision not in known:
        problems.append(f"It was made by a newer version of the application (schema {revision}); update first")
    return {
        "name": path.name,
        "ok": not problems,
        "problems": problems,
        "integrity_ok": integrity,
        "sha256": sha,
        "manifest_present": manifest is not None,
        "checksum_matches": sha_ok,
        "revision": revision,
        "needs_upgrade": bool(revision in known and revision != known[-1]),
        "size": path.stat().st_size,
    }


# ------------------------------------------------------------------ restore
def _copy_pages(src: sqlite3.Connection, dst: sqlite3.Connection, wait_s: float) -> None:
    """The online backup, but never waiting forever: if the database stays busy past `wait_s`, give up cleanly.

    (Python's own backup call retries a busy database without limit, which would hang a web request.) The copy is one
    all-or-nothing write to the destination, so giving up leaves it exactly as it was.
    """
    deadline = time.monotonic() + wait_s

    def watch(status: int, remaining: int, total: int) -> None:
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
            raise TimeoutError

    try:
        src.backup(dst, pages=-1, progress=watch, sleep=0.1)
    except TimeoutError as exc:
        raise BackupError(
            f"The database stayed busy for {wait_s:.0f} seconds, so nothing was changed. Try again in a moment"
            " (or stop the application first)"
        ) from exc


def _copy_into(source: Path, destination: Path) -> None:
    """Replace the contents of `destination` with `source` (online backup API, all or nothing)."""
    src = _readonly(source)
    dst = sqlite3.connect(destination, timeout=RESTORE_WAIT_S)
    try:
        _copy_pages(src, dst, RESTORE_WAIT_S)
    finally:
        dst.close()
        src.close()


def restore_backup(database_url: str, backup_path: Path, backup_dir: str | Path) -> dict:
    """Put a backup back as the live database, and prove the result matches the backup exactly.

    1. refuse a backup that is damaged, altered or from a newer version;
    2. take a safety copy of the current data (so the restore itself can be undone);
    3. copy the backup over the live database;
    4. check integrity and compare every table with the backup; if anything differs, put the safety copy back.
    """
    live = sqlite_file(database_url)
    backup_path = Path(backup_path)
    check = verify_backup(backup_path)
    if not check["ok"]:
        raise BackupError("; ".join(check["problems"]))
    safety = (
        create_backup(database_url, backup_dir, "pre-restore", note=f"taken before restoring {backup_path.name}")
        if live.is_file()
        else None
    )
    _copy_into(backup_path, live)  # if this gives up (database busy) nothing was written, so there is nothing to undo
    try:
        proof = compare(live, backup_path)
        exact = all(t["match"] for t in proof["tables"])  # after a restore even the activity tables must match
        if not (_integrity_ok(live) and exact):
            raise BackupError("the restored data did not match the backup")
    except Exception as exc:
        if safety is not None:
            _copy_into(safety.path, live)
        raise BackupError(f"Restore failed and the previous data was put back ({exc})") from exc
    return {
        "restored": backup_path.name,
        "safety_backup": safety.path.name if safety else None,
        "revision": check["revision"],
        "needs_upgrade": check["needs_upgrade"],
        "proof": proof,
    }
