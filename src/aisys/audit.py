"""Audit logging. Records are added to the caller's session; the caller commits."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aisys.models import AuditEvent


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    entity: str = "",
    entity_id: str | int = "",
    outcome: str = "success",
    detail: dict | None = None,
    ip: str = "",
) -> AuditEvent:
    ev = AuditEvent(
        actor=actor,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
        outcome=outcome,
        detail=detail or {},
        ip=ip,
    )
    db.add(ev)
    return ev
