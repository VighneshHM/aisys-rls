"""Authentication service: staff-card login, password login, sessions.

Rules (see docs/assumptions.md A-04):
  * Only a *staff* card on an active user can create a session.
  * A *patron* card never authenticates; it only identifies a member in circulation transactions.
  * Every attempt, successful or not, is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.auth import security
from aisys.config import Settings
from aisys.integrations.credentials import CardPresentation, CredentialError, CredentialProvider
from aisys.models import AuthSession, Card, User


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class LoginResult:
    token: str
    user: User
    expires_at: datetime


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; everything we store is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _deny(db: Session, status: int, message: str, *, action: str, actor: str, ip: str, detail: dict) -> AuthError:
    audit.record(db, actor=actor, action=action, outcome="denied", detail=detail, ip=ip)
    db.commit()  # persist the audit trail even though the request fails
    return AuthError(status, message)


def _open_session(db: Session, settings: Settings, user: User, method: str, ip: str) -> LoginResult:
    token = security.new_token()
    expires = datetime.now(UTC) + timedelta(minutes=settings.session_ttl_minutes)
    db.add(AuthSession(token_hash=security.token_hash(token), user_id=user.id, method=method, expires_at=expires))
    audit.record(db, actor=user.username, action=f"auth.login.{method}", entity="user", entity_id=user.id, ip=ip)
    db.commit()
    return LoginResult(token=token, user=user, expires_at=expires)


def login_with_card(
    db: Session, settings: Settings, provider: CredentialProvider, presentation: CardPresentation, ip: str = ""
) -> LoginResult:
    try:
        uid = provider.verify(presentation)
    except CredentialError as exc:
        raise _deny(
            db,
            400,
            "Card could not be read",
            action="auth.login.card",
            actor="anonymous",
            ip=ip,
            detail={"reason": str(exc)},
        ) from exc

    card = db.scalar(select(Card).where(Card.uid == uid))
    detail = {"uid": security.card_hint(uid)}  # the log shows which card, never the whole credential
    if card is None:
        raise _deny(
            db,
            401,
            "Unknown card",
            action="auth.login.card",
            actor="anonymous",
            ip=ip,
            detail={**detail, "reason": "unknown_card"},
        )
    if card.kind != "staff":
        raise _deny(
            db,
            403,
            "Patron cards cannot be used to log in",
            action="auth.login.card",
            actor="anonymous",
            ip=ip,
            detail={**detail, "reason": "patron_card_login_attempt"},
        )
    user = card.user
    if not card.active or user is None or not user.active:
        raise _deny(
            db,
            403,
            "Card or account is disabled",
            action="auth.login.card",
            actor="anonymous",
            ip=ip,
            detail={**detail, "reason": "inactive"},
        )
    return _open_session(db, settings, user, "card", ip)


def login_with_password(db: Session, settings: Settings, username: str, password: str, ip: str = "") -> LoginResult:
    user = db.scalar(select(User).where(User.username == username))
    generic = "Invalid username or password"
    if user is None:
        security.verify_password(password, None)
        raise _deny(
            db,
            401,
            generic,
            action="auth.login.password",
            actor="anonymous",
            ip=ip,
            detail={"username": username, "reason": "unknown_user"},
        )
    now = datetime.now(UTC)
    if user.locked_until and as_utc(user.locked_until) > now:
        raise _deny(
            db,
            423,
            "Account temporarily locked",
            action="auth.login.password",
            actor=username,
            ip=ip,
            detail={"reason": "locked"},
        )
    if not user.active or not security.verify_password(password, user.password_hash):
        if user.active:
            user.failed_logins += 1
            if user.failed_logins >= settings.max_failed_logins:
                user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                user.failed_logins = 0
        raise _deny(
            db, 401, generic, action="auth.login.password", actor=username, ip=ip, detail={"reason": "bad_credentials"}
        )
    user.failed_logins = 0
    user.locked_until = None
    return _open_session(db, settings, user, "password", ip)


def resolve_session(db: Session, token: str) -> AuthSession | None:
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == security.token_hash(token)))
    if sess is None or sess.revoked or as_utc(sess.expires_at) <= datetime.now(UTC) or not sess.user.active:
        return None
    return sess


def logout(db: Session, sess: AuthSession, ip: str = "", reason: str = "") -> None:
    sess.revoked = True
    audit.record(
        db,
        actor=sess.user.username,
        action="auth.logout",
        entity="user",
        entity_id=sess.user_id,
        ip=ip,
        detail={"reason": reason} if reason else None,
    )
    db.commit()
