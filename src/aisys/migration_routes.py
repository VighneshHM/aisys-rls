"""HTTP API for data migration (FR10, AC01). All endpoints need the `migration.run` permission."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aisys.config import Settings
from aisys.deps import Principal, client_ip, get_db, get_settings, require
from aisys.migration import mapping as mapping_mod
from aisys.migration import parsing, service
from aisys.models import MigrationBatch, MigrationRow
from aisys.timeutil import iso

router = APIRouter(prefix="/api/migrations", tags=["migration"])
Perm = Depends(require("migration.run"))


class MappingIn(BaseModel):
    mapping: dict[str, str]


def _batch_out(b: MigrationBatch, refreshed: dict | None = None) -> dict:
    out = {
        "id": b.id,
        "filename": b.filename,
        "sha256": b.file_sha256,
        "status": b.status,
        "mapping": b.mapping,
        "headers": b.headers,
        "summary": {k: v for k, v in b.summary.items() if k != "library_stamp"},
        "created_by": b.created_by,
        "created_at": iso(b.created_at),
        "backup": b.backup_path,
        "committed_at": iso(b.committed_at),
        "rolled_back_at": iso(b.rolled_back_at),
        "target_fields": mapping_mod.FIELDS,
        "required_fields": list(mapping_mod.REQUIRED),
    }
    if refreshed:  # shown once: the library changed after this file was checked, so it was checked again
        out["refreshed"] = refreshed
    return out


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.MigrationError as exc:
        raise HTTPException(exc.status, exc.message) from exc


def _get(db: Session, batch_id: int) -> MigrationBatch:
    return _guard(service.get_batch, db, batch_id)


def _get_fresh(db: Session, batch_id: int, principal: Principal, request: Request):
    """Load a batch, re-validating it first if the library changed since it was last checked."""
    batch = _get(db, batch_id)
    refreshed = _guard(service.refresh_if_stale, db, batch, actor=principal.user.username, ip=client_ip(request))
    return batch, refreshed


@router.post("", status_code=201)
async def upload(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    mapping: str | None = Form(default=None),
    principal: Principal = Perm,
    db: Session = Depends(get_db),
):
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > parsing.MAX_BYTES:
            raise HTTPException(413, f"File too large (limit {parsing.MAX_BYTES // (1024 * 1024)} MB)")
        chunks.append(chunk)
    chosen = None
    if mapping:
        try:
            chosen = {str(k): str(v) for k, v in json.loads(mapping).items()}
        except (ValueError, AttributeError) as exc:
            raise HTTPException(422, "mapping must be a JSON object of field -> column") from exc
    batch = _guard(
        service.stage_upload,
        db,
        filename=file.filename or "upload",
        data=b"".join(chunks),
        actor=principal.user.username,
        mapping=chosen,
        ip=client_ip(request),
    )
    return _batch_out(batch)


@router.get("")
def list_batches(_: Principal = Perm, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(MigrationBatch).order_by(MigrationBatch.id.desc()).limit(50)))
    stamp = service.current_stamp(db) if any(b.status == "validated" for b in rows) else ""
    return [
        {
            "id": b.id,
            "filename": b.filename,
            "status": b.status,
            "created_by": b.created_by,
            "created_at": iso(b.created_at),
            "source_rows": b.summary.get("source_rows"),
            "valid": b.summary.get("valid"),
            "invalid": b.summary.get("invalid"),
            "duplicate": b.summary.get("duplicate"),
            "stale": service.is_stale(b, stamp),  # library changed since the check; opening it re-checks
        }
        for b in rows
    ]


@router.get("/{batch_id}")
def get_batch(batch_id: int, request: Request, principal: Principal = Perm, db: Session = Depends(get_db)):
    batch, refreshed = _get_fresh(db, batch_id, principal, request)
    return _batch_out(batch, refreshed)


@router.get("/{batch_id}/rows")
def list_rows(
    batch_id: int,
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal: Principal = Perm,
    db: Session = Depends(get_db),
):
    _get_fresh(db, batch_id, principal, request)
    q = select(MigrationRow).where(MigrationRow.batch_id == batch_id)
    if status:
        q = q.where(MigrationRow.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    page = db.scalars(q.order_by(MigrationRow.row_no).limit(min(max(limit, 1), 500)).offset(max(offset, 0)))
    return {
        "total": total,
        "rows": [
            {"row_no": r.row_no, "status": r.status, "raw": r.raw, "errors": r.errors, "duplicate_of": r.duplicate_of}
            for r in page
        ],
    }


@router.get("/{batch_id}/errors.csv")
def errors_csv(batch_id: int, request: Request, principal: Principal = Perm, db: Session = Depends(get_db)):
    batch, _ = _get_fresh(db, batch_id, principal, request)
    return StreamingResponse(
        service.errors_csv(db, batch),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="migration-{batch_id}-errors.csv"'},
    )


@router.put("/{batch_id}/mapping")
def update_mapping(
    batch_id: int, body: MappingIn, request: Request, principal: Principal = Perm, db: Session = Depends(get_db)
):
    batch = _get(db, batch_id)
    _guard(service.set_mapping, db, batch, body.mapping, actor=principal.user.username, ip=client_ip(request))
    return _batch_out(batch)


@router.post("/{batch_id}/validate")
def revalidate(batch_id: int, request: Request, principal: Principal = Perm, db: Session = Depends(get_db)):
    batch = _get(db, batch_id)
    _guard(service.validate_batch, db, batch, actor=principal.user.username, ip=client_ip(request))
    return _batch_out(batch)


@router.post("/{batch_id}/commit")
def commit(
    batch_id: int,
    request: Request,
    principal: Principal = Perm,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    batch = _get(db, batch_id)
    _guard(service.commit_batch, db, settings, batch, actor=principal.user.username, ip=client_ip(request))
    return _batch_out(batch)


@router.post("/{batch_id}/rollback")
def rollback(batch_id: int, request: Request, principal: Principal = Perm, db: Session = Depends(get_db)):
    batch = _get(db, batch_id)
    _guard(service.rollback_batch, db, batch, actor=principal.user.username, ip=client_ip(request))
    return _batch_out(batch)
