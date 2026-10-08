"""HTTP API for backups: list, create, verify, compare with the live data, restore (and the development drill).

Everything needs `admin.config`. Restoring replaces ALL current data, so it asks for the word RESTORE, takes a safety
copy of the current data first, and proves afterwards that every table matches the backup.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aisys import audit, backup, migrate
from aisys.backup import BackupError
from aisys.config import Settings
from aisys.deps import Principal, client_ip, get_db, get_settings, require
from aisys.devtools import drill

router = APIRouter(prefix="/api/backups", tags=["backups"])
CanAdmin = Depends(require("admin.config"))


def _fail(status: int, message: str) -> HTTPException:
    return HTTPException(status, detail={"message": message, "problems": []})


def backup_path(settings: Settings, name: str) -> Path:
    """The file for a backup name. Only plain names inside the backup folder are accepted (no paths, no '..')."""
    folder = Path(settings.backup_dir).resolve()
    path = (folder / name).resolve()
    if not backup.usable_name(name) or path.parent != folder or not path.is_file():
        raise _fail(404, f"No backup called '{name}'")
    return path


def live_file(settings: Settings) -> Path:
    try:
        return backup.sqlite_file(settings.database_url)
    except BackupError as exc:
        raise _fail(501, str(exc)) from exc


class CreateIn(BaseModel):
    label: str = Field(default="manual", max_length=40)
    note: str = Field(default="", max_length=200)


class RestoreIn(BaseModel):
    confirm: str = ""


@router.get("")
def list_backups(limit: int = 25, offset: int = 0, _: Principal = CanAdmin, settings: Settings = Depends(get_settings)):
    """The backups, newest first, a page at a time (a folder can hold hundreds)."""
    everything = backup.list_backups(settings.backup_dir)
    limit, offset = min(max(limit, 1), 200), max(offset, 0)
    return {
        "backups": everything[offset : offset + limit],
        "total": len(everything),
        "limit": limit,
        "offset": offset,
        "skipped": backup.skipped_files(settings.backup_dir)[:20],  # .db files ignored for their names
        "folder": str(Path(settings.backup_dir)),
        "head": backup.head(),
    }


@router.post("", status_code=201)
def create(
    body: CreateIn,
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        info = backup.create_backup(
            settings.database_url, settings.backup_dir, body.label.strip() or "manual", body.note.strip()
        )
    except BackupError as exc:
        raise _fail(500, str(exc)) from exc
    audit.record(
        db,
        actor=principal.user.username,
        action="backup.create",
        entity="backup",
        entity_id=info.path.name,
        detail={"size": info.size, "sha256": info.sha256},
        ip=client_ip(request),
    )
    db.commit()
    return next(b for b in backup.list_backups(settings.backup_dir) if b["name"] == info.path.name)


@router.get("/{name}/verify")
def verify(name: str, _: Principal = CanAdmin, settings: Settings = Depends(get_settings)):
    return backup.verify_backup(backup_path(settings, name))


@router.get("/{name}/compare")
def compare(name: str, _: Principal = CanAdmin, settings: Settings = Depends(get_settings)):
    """Is the live data identical to this backup? Table by table."""
    path = backup_path(settings, name)
    return {"backup": name, **backup.compare(live_file(settings), path)}


def do_restore(request: Request, settings: Settings, path: Path, actor: str) -> dict:
    """The restore itself, shared by the real endpoint and the drill."""
    maintenance = request.app.state.maintenance
    if maintenance.is_set():  # another restore is already running
        raise _fail(503, "A restore is already in progress. Wait for it to finish.")
    maintenance.set()  # from here until the restore is done, every other request is told to wait
    try:
        try:
            result = backup.restore_backup(settings.database_url, path, settings.backup_dir)
        except BackupError as exc:
            raise _fail(409, str(exc)) from exc
        request.app.state.engine.dispose()  # the app must not keep connections that saw the old data
        return _finish_restore(request, settings, path, actor, result)
    finally:
        maintenance.clear()


def _finish_restore(request: Request, settings: Settings, path: Path, actor: str, result: dict) -> dict:
    """After the data is back: bring an older backup up to the current schema, and log the restore."""
    upgraded = None
    if result["needs_upgrade"]:  # a backup from an older version is brought up to the current schema
        migrate.upgrade(settings.database_url)
        upgraded = backup.head()
    with request.app.state.session_factory() as db:
        audit.record(
            db,
            actor=actor,
            action="backup.restore",
            entity="backup",
            entity_id=path.name,
            detail={
                "safety_backup": result["safety_backup"],
                "tables_match": result["proof"]["data_intact"],
                "upgraded_to": upgraded,
            },
            ip=client_ip(request),
        )
        db.commit()
    return {**result, "upgraded_to": upgraded}


@router.post("/{name}/restore")
def restore(
    name: str,
    body: RestoreIn,
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if body.confirm != "RESTORE":
        raise _fail(422, "Type the word RESTORE to confirm. This replaces all current data with the backup.")
    path = backup_path(settings, name)
    db.close()  # let go of the connection this request used to check who you are
    return do_restore(request, settings, path, principal.user.username)


# ------------------------------------------------------------------ development drill (404 in production)
def _simulator(settings: Settings) -> None:
    if settings.environment == "production":
        raise HTTPException(404, "Not found")


@router.post("/mock/drill", tags=["backup-simulator"])
def run_drill(
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The whole exercise in one go: back up, run a damaging change, show the damage, restore, show the proof."""
    _simulator(settings)
    live = live_file(settings)
    try:
        info = backup.create_backup(
            settings.database_url, settings.backup_dir, "before-drill", "taken by the disaster drill"
        )
    except BackupError as exc:
        raise _fail(500, str(exc)) from exc
    damage = drill.faulty_sync(db)
    damaged = backup.compare(live, info.path)
    db.close()
    restored = do_restore(request, settings, info.path, principal.user.username)
    return {"backup": info.path.name, "damage": damage, "after_damage": damaged, "restored": restored}


@router.post("/mock/damage", tags=["backup-simulator"])
def damage_only(_: Principal = CanAdmin, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Only the damaging change (to practise the compare and restore steps by hand)."""
    _simulator(settings)
    return drill.faulty_sync(db)
