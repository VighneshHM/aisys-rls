"""Permission catalogue and default roles. The code is the source of truth; `sync_rbac` mirrors it to the DB."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys.models import Permission, Role

PERMISSIONS: dict[str, str] = {
    "catalogue.read": "Search and view catalogue records",
    "catalogue.write": "Create and edit catalogue records",
    "member.read": "View members",
    "member.write": "Create and edit members",
    "circulation.checkout": "Check items out",
    "circulation.checkin": "Check items in",
    "circulation.renew": "Renew loans",
    "circulation.override": "Override circulation blocks",
    "rfid.tag": "Tag and re-tag items",
    "inventory.run": "Run inventory and shelf-reading sessions",
    "gate.view": "View gate events",
    "reports.view": "View dashboards and reports",
    "audit.read": "Read the audit log",
    "migration.run": "Run data migrations",
    "acquisitions.manage": "Create and receive purchase orders",
    "serials.manage": "Manage serial subscriptions and receive issues",
    "shelves.manage": "Manage shelves and print labels",
    "admin.users": "Manage staff users and cards",
    "admin.config": "View/change configuration",
    "system.view": "View system health and versions",
}

ROLES: dict[str, tuple[str, list[str]]] = {
    "admin": ("System administrator", list(PERMISSIONS)),
    "librarian": (
        "Librarian",
        [
            "catalogue.read",
            "catalogue.write",
            "member.read",
            "member.write",
            "circulation.checkout",
            "circulation.checkin",
            "circulation.renew",
            "circulation.override",
            "rfid.tag",
            "acquisitions.manage",
            "serials.manage",
            "shelves.manage",
            "inventory.run",
            "gate.view",
            "reports.view",
            "migration.run",
            "system.view",
        ],
    ),
    "circulation_clerk": (
        "Circulation desk clerk",
        [
            "catalogue.read",
            "member.read",
            "circulation.checkout",
            "circulation.checkin",
            "circulation.renew",
        ],
    ),
    "auditor": ("Read-only auditor", ["reports.view", "audit.read", "gate.view", "system.view"]),
}


def sync_rbac(db: Session) -> None:
    """Idempotently upsert permissions and default roles. Never removes admin-made custom roles."""
    existing = {p.code: p for p in db.scalars(select(Permission))}
    for code, desc in PERMISSIONS.items():
        if code not in existing:
            existing[code] = Permission(code=code, description=desc)
            db.add(existing[code])
        else:
            existing[code].description = desc
    db.flush()
    roles = {r.name: r for r in db.scalars(select(Role))}
    for name, (desc, codes) in ROLES.items():
        role = roles.get(name) or Role(name=name)
        role.description = desc
        role.permissions = [existing[c] for c in codes]
        db.add(role)
    db.commit()
