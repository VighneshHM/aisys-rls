"""HTTP API for the foundation layer: auth, admin, audit, health."""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shelfsense import __version__, audit, migrate
from shelfsense.auth import security, service
from shelfsense.config import Settings
from shelfsense.deps import (
    COOKIE_NAME,
    Principal,
    client_ip,
    get_db,
    get_principal,
    get_provider,
    get_settings,
    require,
    within_own_rights,
)
from shelfsense.integrations.credentials import CardPresentation, CredentialProvider
from shelfsense.models import AuditEvent, Card, Role, User
from shelfsense.rfid import tagging
from shelfsense.timeutil import iso

router = APIRouter()
STARTED_AT = datetime.now(UTC)


# ---------- schemas ----------
class CardLoginIn(BaseModel):
    uid: str = Field(min_length=1, max_length=64)
    proof: str | None = None


class PasswordLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateIn(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9._-]{3,64}$")
    display_name: str = Field(min_length=1, max_length=120)
    role: str
    password: str | None = Field(default=None, min_length=10, max_length=256)


class CardIssueIn(BaseModel):
    uid: str = Field(pattern=r"^[A-Z0-9]{6,32}$")
    kind: str = Field(pattern=r"^(staff|patron)$")
    username: str | None = None  # required for staff cards
    member_ref: str | None = Field(default=None, max_length=64)  # required for patron cards


def _login_response(result: service.LoginResult, response: Response, settings: Settings) -> dict:
    response.set_cookie(
        COOKIE_NAME,
        result.token,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        max_age=settings.session_ttl_minutes * 60,
    )
    return {
        "token": result.token,
        "expires_at": iso(result.expires_at),
        "user": _user_out(result.user),
    }


def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role.name,
        "permissions": sorted(p.code for p in u.role.permissions),
        "active": u.active,
    }


def _handle(exc: service.AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


# ---------- auth ----------
@router.post("/api/auth/card-login", tags=["auth"])
def card_login(
    body: CardLoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider: CredentialProvider = Depends(get_provider),
):
    try:
        result = service.login_with_card(
            db, settings, provider, CardPresentation(body.uid, body.proof), client_ip(request)
        )
    except service.AuthError as exc:
        raise _handle(exc) from exc
    return _login_response(result, response, settings)


@router.post("/api/auth/login", tags=["auth"])
def password_login(
    body: PasswordLoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        result = service.login_with_password(db, settings, body.username, body.password, client_ip(request))
    except service.AuthError as exc:
        raise _handle(exc) from exc
    return _login_response(result, response, settings)


@router.post("/api/auth/logout", tags=["auth"])
def logout(
    request: Request, response: Response, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
):
    service.logout(db, db.merge(principal.session), client_ip(request))
    response.delete_cookie(COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/api/auth/me", tags=["auth"])
def me(principal: Principal = Depends(get_principal)):
    return _user_out(principal.user)


# ---------- admin ----------
@router.get("/api/admin/users", tags=["admin"])
def list_users(_: Principal = Depends(require("admin.users")), db: Session = Depends(get_db)):
    return [_user_out(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post("/api/admin/users", status_code=201, tags=["admin"])
def create_user(
    body: UserCreateIn,
    request: Request,
    principal: Principal = Depends(require("admin.users")),
    db: Session = Depends(get_db),
):
    role = db.scalar(select(Role).where(Role.name == body.role))
    if role is None:
        raise HTTPException(422, f"Unknown role: {body.role}")
    within_own_rights(db, request, principal, (p.code for p in role.permissions), f"give the role '{role.name}'")
    user = User(
        username=body.username,
        display_name=body.display_name,
        role_id=role.id,
        password_hash=security.hash_password(body.password) if body.password else None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Username already exists") from exc
    audit.record(
        db,
        actor=principal.user.username,
        action="admin.user.create",
        entity="user",
        entity_id=user.id,
        detail={"username": user.username, "role": role.name},
        ip=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.post("/api/admin/cards", status_code=201, tags=["admin"])
def issue_card(
    body: CardIssueIn,
    request: Request,
    principal: Principal = Depends(require("admin.users")),
    db: Session = Depends(get_db),
):
    if body.kind == "staff":
        user = db.scalar(select(User).where(User.username == (body.username or "")))
        if user is None:
            raise HTTPException(422, "Staff card requires an existing username")
        if not user.active:
            raise HTTPException(409, "That staff member is deactivated; activate them before issuing a card.")
        within_own_rights(
            db, request, principal, (p.code for p in user.role.permissions), f"issue a card to '{user.username}'"
        )
        card = Card(uid=body.uid, kind="staff", user_id=user.id)
    else:
        if not body.member_ref:
            raise HTTPException(422, "Patron card requires member_ref")
        # the same member-record validation as the circulation desk (FR05), so the two paths cannot drift apart
        chk = tagging.validate_member_tagging(db, body.member_ref, body.uid)
        if not chk.ok:
            codes = {p["code"] for p in chk.problems}
            status = 409 if codes <= {"card_in_use"} else 422  # an unknown member is a bad request here, as before
            raise HTTPException(status, "; ".join(p["message"] for p in chk.problems))
        assert chk.member is not None
        if chk.current_card is not None:  # a member has one working patron card: the old one is switched off
            chk.current_card.active = False
        card = Card(uid=chk.uid, kind="patron", member_ref=chk.member.member_no)
    db.add(card)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Card UID already issued") from exc
    audit.record(
        db,
        actor=principal.user.username,
        action="admin.card.issue",
        entity="card",
        entity_id=card.id,
        detail={"uid": security.card_hint(card.uid), "kind": card.kind},
        ip=client_ip(request),
    )
    db.commit()
    return {"id": card.id, "uid": card.uid, "kind": card.kind}


# ---------- audit ----------
@router.get("/api/audit", tags=["audit"])
def read_audit(
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
    _: Principal = Depends(require("audit.read")),
    db: Session = Depends(get_db),
):
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(max(limit, 1), 1000))
    if actor:
        q = q.where(AuditEvent.actor == actor)
    if action:
        q = q.where(AuditEvent.action.like(f"{action}%"))
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    return [
        {
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
        for e in db.scalars(q)
    ]


# ---------- health / system ----------
@router.get("/health", tags=["system"])
def liveness():
    return {"status": "ok"}


@router.get("/health/ready", tags=["system"])
def readiness(request: Request, response: Response):
    checks: dict[str, dict] = {}
    engine = request.app.state.engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
        current, head = migrate.current_revision(engine), migrate.head_revision()
        checks["schema"] = {"ok": current == head, "current": current, "head": head}
    except Exception as exc:  # noqa: BLE001 - health endpoint must never raise
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    ok = all(c.get("ok") for c in checks.values()) and "schema" in checks
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "degraded", "checks": checks}


@router.get("/api/system/info", tags=["system"])
def system_info(
    request: Request, _: Principal = Depends(require("system.view")), settings: Settings = Depends(get_settings)
):
    engine = request.app.state.engine
    return {
        "app": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "schema_revision": migrate.current_revision(engine),
        "schema_head": migrate.head_revision(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((datetime.now(UTC) - STARTED_AT).total_seconds()),
    }
