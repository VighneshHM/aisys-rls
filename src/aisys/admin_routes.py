"""HTTP API behind the Audit log and Administration pages.

Audit (`audit.read`): a searchable, paged view of the append-only log, and a CSV export that neutralises formulas.
Administration (`admin.users`): see staff and their cards, activate or deactivate accounts and cards, unlock a locked
account, change a role, and define custom roles (the four built-in roles are fixed by the release and reset at every
start by `sync_rbac`). Creating users and issuing cards stays in `routes.py`. Two safety rules protect the system from
locking itself out: you cannot deactivate or demote yourself, and the last active administrator cannot be removed.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.auth.permissions import PERMISSIONS, ROLES
from aisys.auth.security import card_hint
from aisys.deps import Principal, client_ip, get_db, require
from aisys.models import AuditEvent, AuthSession, Card, Permission, Role, User
from aisys.timeutil import aware, iso

router = APIRouter(tags=["admin"])
CanAudit = Depends(require("audit.read"))
CanAdmin = Depends(require("admin.users"))
EXPORT_LIMIT = 5000
MAX_PAGE = 100


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filtered(actor: str | None, action: str | None, outcome: str | None, text: str | None):
    q = select(AuditEvent)
    if actor:
        q = q.where(AuditEvent.actor == actor)
    if action:
        q = q.where(AuditEvent.action.like(_escape_like(action.strip()) + "%", escape="\\"))
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    if text and text.strip():
        pattern = "%" + _escape_like(text.strip()) + "%"
        q = q.where(
            AuditEvent.entity_id.like(pattern, escape="\\")
            | AuditEvent.entity.like(pattern, escape="\\")
            | AuditEvent.ip.like(pattern, escape="\\")
        )
    return q


def _event(e: AuditEvent) -> dict:
    return {
        "id": e.id,
        "ts": iso(e.ts),
        "actor": e.actor,
        "action": e.action,
        "entity": e.entity,
        "entity_id": e.entity_id,
        "outcome": e.outcome,
        "detail": e.detail,
        "ip": e.ip,
    }


@router.get("/api/audit/events", tags=["audit"])
def audit_events(
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    _: Principal = CanAudit,
    db: Session = Depends(get_db),
):
    """Newest first, a page at a time, with the total so a pager can be drawn."""
    limit, offset = min(max(limit, 1), MAX_PAGE), max(offset, 0)
    base = _filtered(actor, action, outcome, q)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.scalars(base.order_by(AuditEvent.id.desc()).limit(limit).offset(offset))
    actors = list(db.scalars(select(AuditEvent.actor).distinct().order_by(AuditEvent.actor).limit(200)))
    return {"events": [_event(e) for e in rows], "total": total, "limit": limit, "offset": offset, "actors": actors}


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return (
        "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
    )  # a spreadsheet would run it as a formula


@router.get("/api/audit/export.csv", tags=["audit"])
def audit_export(
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    q: str | None = None,
    _: Principal = CanAudit,
    db: Session = Depends(get_db),
):
    rows = db.scalars(_filtered(actor, action, outcome, q).order_by(AuditEvent.id.desc()).limit(EXPORT_LIMIT))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "time", "actor", "action", "entity", "entity_id", "outcome", "ip", "detail"])
    for e in rows:
        writer.writerow(
            [
                e.id,
                iso(e.ts),
                _cell(e.actor),
                _cell(e.action),
                _cell(e.entity),
                _cell(e.entity_id),
                e.outcome,
                _cell(e.ip),
                _cell(json.dumps(e.detail, sort_keys=True)),
            ]
        )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- administration
def _person(u: User, cards: list[Card], now: datetime) -> dict:
    locked = bool(u.locked_until and aware(u.locked_until) > now)
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role.name,
        "active": u.active,
        "locked": locked,
        "locked_until": iso(u.locked_until) if locked else None,
        "has_password": bool(u.password_hash),
        "created_at": iso(u.created_at),
        "cards": [
            {"id": c.id, "uid_hint": card_hint(c.uid), "active": c.active, "issued_at": iso(c.issued_at)} for c in cards
        ],
    }


@router.get("/api/admin/people", tags=["admin"])
def people(_: Principal = CanAdmin, db: Session = Depends(get_db)):
    now = datetime.now(UTC)
    cards: dict[int, list[Card]] = {}
    for c in db.scalars(select(Card).where(Card.kind == "staff").order_by(Card.id)):
        if c.user_id is not None:
            cards.setdefault(c.user_id, []).append(c)
    users = db.scalars(select(User).order_by(User.username))
    roles = db.scalars(select(Role).order_by(Role.name))
    return {
        "people": [_person(u, cards.get(u.id, []), now) for u in users],
        "roles": [{"name": r.name, "description": r.description} for r in roles],
    }


class UserPatch(BaseModel):
    active: bool | None = None
    unlock: bool = False
    role: str | None = None


class CardPatch(BaseModel):
    active: bool


def _active_admins(db: Session, excluding: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .join(Role, User.role_id == Role.id)
            .where(Role.name == "admin", User.active.is_(True), User.id != excluding)
        )
        or 0
    )


@router.patch("/api/admin/users/{user_id}", tags=["admin"])
def patch_user(
    user_id: int,
    body: UserPatch,
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    me = principal.user.id == user.id
    changes: dict = {}
    if body.active is not None and body.active != user.active:
        if me and not body.active:
            raise HTTPException(409, "You cannot deactivate your own account.")
        if not body.active and user.role.name == "admin" and _active_admins(db, user.id) == 0:
            raise HTTPException(409, "This is the last active administrator and cannot be deactivated.")
        user.active = body.active
        changes["active"] = body.active
        if not body.active:  # end any session they have open right now
            for s in db.scalars(
                select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked.is_(False))
            ):
                s.revoked = True
    if body.role is not None and body.role != user.role.name:
        role = db.scalar(select(Role).where(Role.name == body.role))
        if role is None:
            raise HTTPException(422, f"Unknown role: {body.role}")
        if me:
            raise HTTPException(409, "You cannot change your own role.")
        if user.role.name == "admin" and user.active and _active_admins(db, user.id) == 0:
            raise HTTPException(409, "This is the last active administrator and cannot be demoted.")
        changes["role"] = {"from": user.role.name, "to": role.name}
        user.role_id = role.id
        user.role = role
    if body.unlock and (user.locked_until or user.failed_logins):
        user.locked_until = None
        user.failed_logins = 0
        changes["unlocked"] = True
    if changes:
        audit.record(
            db,
            actor=principal.user.username,
            action="admin.user.update",
            entity="user",
            entity_id=user.id,
            detail={"username": user.username, **changes},
            ip=client_ip(request),
        )
        db.commit()
    return _person(
        user, list(db.scalars(select(Card).where(Card.user_id == user.id, Card.kind == "staff"))), datetime.now(UTC)
    )


# ---------------------------------------------------------------- roles
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


def _role(r: Role, holders: int) -> dict:
    return {
        "name": r.name,
        "description": r.description,
        "built_in": r.name in ROLES,
        "permissions": sorted(p.code for p in r.permissions),
        "holders": holders,
    }


def _holders(db: Session) -> dict[int, int]:
    return {role_id: n for role_id, n in db.execute(select(User.role_id, func.count()).group_by(User.role_id))}


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(min_length=1, max_length=len(PERMISSIONS))


class RolePatch(BaseModel):
    description: str | None = Field(default=None, max_length=200)
    permissions: list[str] | None = Field(default=None, min_length=1, max_length=len(PERMISSIONS))


def _permissions(db: Session, codes: list[str]) -> list[Permission]:
    unknown = sorted(set(codes) - set(PERMISSIONS))
    if unknown:
        raise HTTPException(422, f"Unknown permission: {', '.join(unknown)}")
    return list(db.scalars(select(Permission).where(Permission.code.in_(set(codes))).order_by(Permission.code)))


def _custom_role(db: Session, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise HTTPException(404, "No such role")
    if role.name in ROLES:
        raise HTTPException(409, f"'{role.name}' is a built-in role: its permissions are set by the software release.")
    return role


@router.get("/api/admin/roles", tags=["admin"])
def roles(_: Principal = CanAdmin, db: Session = Depends(get_db)):
    """Every role with its permissions, and the catalogue of permissions a custom role can be given."""
    holders = _holders(db)
    return {
        "roles": [_role(r, holders.get(r.id, 0)) for r in db.scalars(select(Role).order_by(Role.name))],
        "permissions": [{"code": c, "description": d} for c, d in PERMISSIONS.items()],
    }


@router.post("/api/admin/roles", status_code=201, tags=["admin"])
def create_role(body: RoleIn, request: Request, principal: Principal = CanAdmin, db: Session = Depends(get_db)):
    """A custom role: a name and a chosen set of permissions. Built-in roles cannot be redefined."""
    name = body.name.strip().lower()
    if not ROLE_NAME_RE.match(name):
        raise HTTPException(422, "A role name is 3 to 32 lowercase letters, digits or _, starting with a letter")
    if db.scalar(select(Role.id).where(Role.name == name)) is not None:
        raise HTTPException(409, f"There is already a role called '{name}'")
    role = Role(
        name=name, description=" ".join(body.description.split()), permissions=_permissions(db, body.permissions)
    )
    db.add(role)
    db.flush()
    audit.record(
        db, actor=principal.user.username, action="admin.role.create", entity="role", entity_id=name,
        detail={"permissions": sorted(p.code for p in role.permissions)}, ip=client_ip(request),
    )  # fmt: skip
    db.commit()
    return _role(role, 0)


@router.patch("/api/admin/roles/{name}", tags=["admin"])
def patch_role(
    name: str, body: RolePatch, request: Request, principal: Principal = CanAdmin, db: Session = Depends(get_db)
):
    """Change a custom role's description or permissions. People holding it get the new rights on their next request."""
    role = _custom_role(db, name)
    changes: dict = {}
    if body.description is not None and " ".join(body.description.split()) != role.description:
        role.description = " ".join(body.description.split())
        changes["description"] = role.description
    if body.permissions is not None:
        before, wanted = {p.code for p in role.permissions}, _permissions(db, body.permissions)
        after = {p.code for p in wanted}
        if after != before:
            if principal.user.role_id == role.id and "admin.users" in before - after:
                raise HTTPException(409, "You cannot take 'admin.users' away from your own role.")
            role.permissions = wanted
            changes["added"], changes["removed"] = sorted(after - before), sorted(before - after)
    if changes:
        audit.record(
            db, actor=principal.user.username, action="admin.role.update", entity="role", entity_id=role.name,
            detail=changes, ip=client_ip(request),
        )  # fmt: skip
        db.commit()
    return _role(role, _holders(db).get(role.id, 0))


@router.delete("/api/admin/roles/{name}", tags=["admin"])
def delete_role(name: str, request: Request, principal: Principal = CanAdmin, db: Session = Depends(get_db)):
    """Remove a custom role that nobody holds. Its history stays in the audit log."""
    role = _custom_role(db, name)
    holders = _holders(db).get(role.id, 0)
    if holders:
        raise HTTPException(
            409, f"{holders} staff member(s) still have the role '{role.name}': give them another first."
        )
    audit.record(
        db, actor=principal.user.username, action="admin.role.delete", entity="role", entity_id=role.name,
        detail={"permissions": sorted(p.code for p in role.permissions)}, ip=client_ip(request),
    )  # fmt: skip
    db.delete(role)
    db.commit()
    return {"deleted": role.name}


@router.patch("/api/admin/cards/{card_id}", tags=["admin"])
def patch_card(
    card_id: int,
    body: CardPatch,
    request: Request,
    principal: Principal = CanAdmin,
    db: Session = Depends(get_db),
):
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(404, "No such card")
    if card.active != body.active:
        card.active = body.active
        audit.record(
            db,
            actor=principal.user.username,
            action="admin.card.activate" if body.active else "admin.card.deactivate",
            entity="card",
            entity_id=card.id,
            detail={"uid_hint": card_hint(card.uid), "kind": card.kind},
            ip=client_ip(request),
        )
        db.commit()
    return {"id": card.id, "kind": card.kind, "uid_hint": card_hint(card.uid), "active": card.active}
