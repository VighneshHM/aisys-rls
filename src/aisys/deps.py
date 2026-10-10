"""FastAPI dependencies: DB session, current principal, permission checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from aisys import audit
from aisys.auth import service
from aisys.config import Settings
from aisys.integrations.credentials import CredentialProvider
from aisys.models import AuthSession, User

COOKIE_NAME = "aisys_session"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_provider(request: Request) -> CredentialProvider:
    return request.app.state.credential_provider


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@dataclass
class Principal:
    user: User
    session: AuthSession
    permissions: frozenset[str]


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def get_principal_optional(request: Request, db: Session = Depends(get_db)) -> Principal | None:
    token = _token_from(request)
    sess = service.resolve_session(db, token) if token else None
    if sess is None:
        return None
    return Principal(sess.user, sess, frozenset(p.code for p in sess.user.role.permissions))


def get_principal(principal: Principal | None = Depends(get_principal_optional)) -> Principal:
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def within_own_rights(db: Session, request: Request, principal: Principal, codes, what: str) -> None:
    """403 (audited) when an action would hand out, or reach, rights the acting person does not hold themselves.

    Holding ``admin.users`` lets someone manage staff, but never give anyone (themselves included, through a second
    account or a custom role) more than they have, nor change the account, cards or role of someone who has more
    (D-071).
    """
    beyond = sorted(set(codes) - principal.permissions)
    if not beyond:
        return
    audit.record(
        db,
        actor=principal.user.username,
        action="authz.denied",
        outcome="denied",
        detail={"beyond_own_rights": beyond, "what": what, "path": request.url.path},
        ip=client_ip(request),
    )
    db.commit()
    raise HTTPException(
        status_code=403, detail=f"You cannot {what}: it needs rights you do not hold ({', '.join(beyond)})"
    )


def require(permission: str) -> Callable[..., Principal]:
    """Dependency factory: 401 if not logged in, 403 (and an audit record) if the role lacks the permission."""

    def dependency(
        request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
    ) -> Principal:
        if permission not in principal.permissions:
            audit.record(
                db,
                actor=principal.user.username,
                action="authz.denied",
                outcome="denied",
                detail={"permission": permission, "path": request.url.path},
                ip=client_ip(request),
            )
            db.commit()
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return principal

    dependency.permission = permission  # type: ignore[attr-defined]  # lets the API reference list who may call what
    return dependency
