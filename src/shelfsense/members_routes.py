"""HTTP API behind the Patrons page: see every member, remove or restore one, erase a removed member's details.

Seeing the list needs `member.read`. Removing and restoring needs `member.remove` (librarians and administrators).
Erasing personal details is irreversible and needs `member.erase` (administrators). Nothing here deletes a loan, a
fine or an audit event; see `circulation/member_admin.py` for the rules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shelfsense.circulation import member_admin, service
from shelfsense.circulation.connector import CirculationConfig
from shelfsense.config import Settings
from shelfsense.deps import Principal, client_ip, get_db, get_settings, require

router = APIRouter(tags=["members"])


class RemoveIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class EraseIn(BaseModel):
    confirm: str = Field(default="", max_length=64)


def _http(exc: service.CircError) -> HTTPException:
    return HTTPException(exc.status, detail={"message": exc.message, "code": exc.code, "problems": []})


def _config(settings: Settings = Depends(get_settings)) -> CirculationConfig:
    return settings.circulation


@router.get("/api/members")
def list_members(
    q: str = "",
    status: str = "",
    loans: bool = False,
    fines: bool = False,
    limit: int = 25,
    offset: int = 0,
    _: Principal = Depends(require("member.read")),
    db: Session = Depends(get_db),
) -> dict:
    """Every member, a page at a time. Removed members only appear with `status=removed`."""
    return member_admin.list_members(db, q=q, status=status, loans=loans, fines=fines, limit=limit, offset=offset)


@router.get("/api/members/counts")
def member_counts(_: Principal = Depends(require("member.read")), db: Session = Depends(get_db)) -> dict:
    return member_admin.counts(db)


@router.post("/api/members/{member_no}/remove")
def remove_member(
    member_no: str,
    body: RemoveIn,
    request: Request,
    principal: Principal = Depends(require("member.remove")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(_config),
) -> dict:
    try:
        member = member_admin.remove_member(
            db, member_no, body.reason, actor=principal.user.username, ip=client_ip(request)
        )
    except service.CircError as exc:
        raise _http(exc) from exc
    return service.member_out(db, member, config, with_loans=False)


@router.post("/api/members/{member_no}/restore")
def restore_member(
    member_no: str,
    request: Request,
    principal: Principal = Depends(require("member.remove")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(_config),
) -> dict:
    try:
        member = member_admin.restore_member(db, member_no, actor=principal.user.username, ip=client_ip(request))
    except service.CircError as exc:
        raise _http(exc) from exc
    return service.member_out(db, member, config, with_loans=False)


@router.post("/api/members/{member_no}/erase")
def erase_member(
    member_no: str,
    body: EraseIn,
    request: Request,
    principal: Principal = Depends(require("member.erase")),
    db: Session = Depends(get_db),
    config: CirculationConfig = Depends(_config),
) -> dict:
    try:
        member = member_admin.erase_member(
            db, member_no, body.confirm, actor=principal.user.username, ip=client_ip(request)
        )
    except service.CircError as exc:
        raise _http(exc) from exc
    return service.member_out(db, member, config, with_loans=False)
