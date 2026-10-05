"""Server-rendered pages: sign-in and the staff dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aisys import __version__, migrate
from aisys.cli import DEMO_PATRON_CARDS, DEMO_USERS
from aisys.config import Settings
from aisys.deps import Principal, get_db, get_principal_optional, get_settings
from aisys.models import AuditEvent, AuthSession, Card, User
from aisys.routes import STARTED_AT

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
router = APIRouter(include_in_schema=False)

# (key, label, icon, href, permission needed to see it, implemented yet?)
NAV = [
    ("dashboard", "Dashboard", "dash", "/dashboard", None, True),
    ("catalogue", "Catalogue", "book", "#", "catalogue.read", False),
    ("circulation", "Circulation", "swap", "#", "circulation.checkout", False),
    ("tagging", "RFID tagging", "tag", "#", "rfid.tag", False),
    ("inventory", "Inventory", "scan", "#", "inventory.run", False),
    ("gate", "Gate events", "gate", "#", "gate.view", False),
    ("reports", "Reports", "chart", "#", "reports.view", False),
    ("migration", "Data migration", "upload", "#", "migration.run", False),
    ("audit", "Audit log", "list", "#", "audit.read", False),
    ("admin", "Administration", "users", "#", "admin.users", False),
]

ROLE_LABELS = {
    "admin": "Administrator",
    "librarian": "Librarian",
    "circulation_clerk": "Circulation clerk",
    "auditor": "Auditor",
}


def _context(request: Request, settings: Settings, **extra) -> dict:
    return {"app_name": settings.app_name, "version": __version__, "environment": settings.environment, **extra}


def _greeting() -> str:
    hour = datetime.now().hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def _uptime() -> str:
    secs = int((datetime.now(UTC) - STARTED_AT).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return " ".join(
        p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m or h or d else "", f"{s}s") if p
    )


def _group_permissions(perms: frozenset[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for p in sorted(perms):
        groups[p.split(".")[0].replace("_", " ").title()].append(p)
    return sorted(groups.items())


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if get_principal_optional(request, db) is not None:
        return RedirectResponse("/dashboard")
    demo = []
    if settings.environment != "production":
        demo = [{"uid": uid, "kind": "staff", "label": role.replace("_", " ")} for _, _, role, uid in DEMO_USERS]
        demo += [{"uid": uid, "kind": "patron", "label": "patron"} for uid, _ in DEMO_PATRON_CARDS[:1]]
    return TEMPLATES.TemplateResponse(request, "login.html", _context(request, settings, demo=demo))


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    if principal is None:
        return RedirectResponse("/")
    user, perms = principal.user, principal.permissions

    since = datetime.now(UTC) - timedelta(hours=24)
    stats = [
        {
            "icon": "users",
            "label": "Staff accounts",
            "value": db.scalar(select(func.count(User.id)).where(User.active)),
        },
        {"icon": "card", "label": "Cards issued", "value": db.scalar(select(func.count(Card.id)).where(Card.active))},
        {
            "icon": "key",
            "label": "Active sessions",
            "value": db.scalar(
                select(func.count(AuthSession.id)).where(
                    AuthSession.revoked.is_(False), AuthSession.expires_at > datetime.now(UTC)
                )
            ),
        },
        {
            "icon": "pulse",
            "label": "Events (24 h)",
            "value": db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.ts >= since)),
        },
    ]

    recent = None
    if "audit.read" in perms:
        rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(8))
        recent = [
            {
                "when": (e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=UTC)).astimezone().strftime("%d %b, %H:%M"),
                "actor": e.actor,
                "action": e.action,
                "outcome": e.outcome,
            }
            for e in rows
        ]

    engine = request.app.state.engine
    try:
        db.execute(text("SELECT 1"))
        current, head = migrate.current_revision(engine), migrate.head_revision()
        health = {"ok": current == head, "db": True, "current": current, "head": head}
    except Exception:  # noqa: BLE001 - page must still render when the DB is unhealthy
        health = {"ok": False, "db": False, "current": None, "head": None}
    health["uptime"] = _uptime()

    nav = [
        {"key": k, "label": label, "icon": icon, "href": href, "live": live}
        for k, label, icon, href, perm, live in NAV
        if perm is None or perm in perms
    ]
    initials = "".join(w[0] for w in user.display_name.split()[:2]).upper()
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            settings,
            user=user,
            role_label=ROLE_LABELS.get(user.role.name, user.role.name.replace("_", " ").title()),
            initials=initials,
            greeting=_greeting(),
            perms=sorted(perms),
            perm_groups=_group_permissions(perms),
            stats=stats,
            recent=recent,
            health=health,
            nav=nav,
            active="dashboard",
        ),
    )
