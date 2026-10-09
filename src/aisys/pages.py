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
from aisys.cli import DEMO_MEMBERS, DEMO_USERS
from aisys.config import Settings
from aisys.deps import Principal, get_db, get_principal_optional, get_settings
from aisys.migration.mapping import FIELDS, REQUIRED
from aisys.models import AuditEvent, AuthSession, Card, Item, Loan, RfidTag, User
from aisys.routes import STARTED_AT

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))
router = APIRouter(include_in_schema=False)

# (key, label, icon, href, permission needed to see it, implemented yet?)
NAV = [
    ("dashboard", "Dashboard", "dash", "/dashboard", None, True),
    ("catalogue", "Catalogue", "book", "/catalogue", "catalogue.read", True),
    ("opac", "Public catalogue", "scan", "/opac", None, True),
    ("circulation", "Circulation", "swap", "/circulation", "circulation.checkout", True),
    ("tagging", "RFID tagging", "tag", "/tagging", "rfid.tag", True),
    ("registry", "Tag registry", "list", "/tags", "catalogue.read", True),
    ("inventory", "Inventory", "scan", "/inventory", "inventory.run", True),
    ("shelves", "Shelves", "book", "/shelves", "catalogue.read", True),
    ("labels", "Labels", "tag", "/labels", "catalogue.write", True),
    ("acquisitions", "Acquisitions", "upload", "/acquisitions", "acquisitions.manage", True),
    ("serials", "Serials", "list", "/serials", "serials.manage", True),
    ("gate", "Gate events", "gate", "/gate", "gate.view", True),
    ("reports", "Reports", "chart", "/reports", "reports.view", True),
    ("migration", "Data migration", "upload", "/migration", "migration.run", True),
    ("audit", "Audit log", "list", "/audit", "audit.read", True),
    ("backups", "Backups", "shield", "/backups", "admin.config", True),
    ("updates", "Updates", "server", "/updates", "admin.config", True),
    ("admin", "Administration", "users", "/admin", "admin.users", True),
]

ROLE_LABELS = {
    "admin": "Administrator",
    "librarian": "Librarian",
    "circulation_clerk": "Circulation clerk",
    "auditor": "Auditor",
}


STATIC_DIR = Path(__file__).parent / "web" / "static"


def asset_version() -> str:
    """Changes whenever any static file changes, so a browser never keeps running a stale script or stylesheet."""
    newest = max((f.stat().st_mtime_ns for f in STATIC_DIR.iterdir() if f.is_file()), default=0)
    return f"{__version__}-{newest // 1_000_000:x}"


def _context(request: Request, settings: Settings, **extra) -> dict:
    return {
        "app_name": settings.app_name,
        "version": __version__,
        "asset_v": asset_version(),
        "environment": settings.environment,
        **extra,
    }


def _shell(request: Request, settings: Settings, principal: Principal, active: str, **extra) -> dict:
    """Context shared by every signed-in page: navigation filtered by permission, identity, version."""
    user = principal.user
    nav = [
        {"key": k, "label": label, "icon": icon, "href": href, "live": live}
        for k, label, icon, href, perm, live in NAV
        if perm is None or perm in principal.permissions
    ]
    return _context(
        request,
        settings,
        user=user,
        role_label=ROLE_LABELS.get(user.role.name, user.role.name.replace("_", " ").title()),
        initials="".join(w[0] for w in user.display_name.split()[:2]).upper(),
        nav=nav,
        active=active,
        **extra,
    )


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
        demo += [{"uid": uid, "kind": "patron", "label": "patron"} for uid, *_ in DEMO_MEMBERS[:1]]
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
    perms = principal.permissions

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

    if "catalogue.read" in perms:  # the library at a glance (full reports come with AC08)
        now = datetime.now(UTC)
        tagged = select(func.count(RfidTag.id)).where(RfidTag.status == "active")
        overdue = select(func.count(Loan.id)).where(Loan.returned_at.is_(None), Loan.due_at < now)
        stats += [
            {"icon": "book", "label": "Catalogue items", "value": db.scalar(select(func.count(Item.id)))},
            {"icon": "tag", "label": "Items with a tag", "value": db.scalar(tagged)},
            {
                "icon": "swap",
                "label": "On loan now",
                "value": db.scalar(select(func.count(Loan.id)).where(Loan.returned_at.is_(None))),
            },
            {"icon": "pulse", "label": "Overdue loans", "value": db.scalar(overdue)},
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

    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        _shell(
            request,
            settings,
            principal,
            "dashboard",
            greeting=_greeting(),
            can_reports="reports.view" in perms,
            perms=sorted(perms),
            perm_groups=_group_permissions(perms),
            stats=stats,
            recent=recent,
            health=health,
        ),
    )


@router.get("/migration", response_class=HTMLResponse)
def migration_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    if principal is None:
        return RedirectResponse("/")
    if "migration.run" not in principal.permissions:
        return TEMPLATES.TemplateResponse(
            request, "forbidden.html", _shell(request, settings, principal, "migration"), status_code=403
        )
    return TEMPLATES.TemplateResponse(
        request, "migration.html", _shell(request, settings, principal, "migration", fields=FIELDS, required=REQUIRED)
    )


def _gated_page(request, settings, principal, permission, template, active, **extra):
    """Render a signed-in page if the role has `permission`; otherwise the friendly 'not permitted' page (403)."""
    if principal is None:
        return RedirectResponse("/")
    if permission not in principal.permissions:
        return TEMPLATES.TemplateResponse(
            request, "forbidden.html", _shell(request, settings, principal, active), status_code=403
        )
    return TEMPLATES.TemplateResponse(request, template, _shell(request, settings, principal, active, **extra))


@router.get("/catalogue", response_class=HTMLResponse)
def catalogue_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    perms = principal.permissions if principal else frozenset()
    return _gated_page(
        request,
        settings,
        principal,
        "catalogue.read",
        "catalogue.html",
        "catalogue",
        can_write="catalogue.write" in perms,
        can_tag="rfid.tag" in perms,
    )


@router.get("/tagging", response_class=HTMLResponse)
def tagging_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(
        request,
        settings,
        principal,
        "rfid.tag",
        "tagging.html",
        "tagging",
        simulator=settings.environment != "production",
    )


@router.get("/circulation", response_class=HTMLResponse)
def circulation_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    perms = principal.permissions if principal else frozenset()
    return _gated_page(
        request,
        settings,
        principal,
        "circulation.checkout",
        "circulation.html",
        "circulation",
        simulator=settings.environment != "production",
        can_checkin="circulation.checkin" in perms,
        can_renew="circulation.renew" in perms,
        can_block="member.write" in perms,
        protocol=settings.circulation.ilms_protocol.upper(),
    )


@router.get("/backups", response_class=HTMLResponse)
def backups_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(
        request,
        settings,
        principal,
        "admin.config",
        "backups.html",
        "backups",
        simulator=settings.environment != "production",
    )


@router.get("/shelves", response_class=HTMLResponse)
def shelves_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    perms = principal.permissions if principal else frozenset()
    return _gated_page(
        request, settings, principal, "catalogue.read", "shelves.html", "shelves", can_manage="shelves.manage" in perms
    )


@router.get("/labels", response_class=HTMLResponse)
def labels_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "catalogue.write", "labels.html", "labels")


@router.get("/acquisitions", response_class=HTMLResponse)
def acquisitions_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "acquisitions.manage", "acquisitions.html", "acquisitions")


@router.get("/serials", response_class=HTMLResponse)
def serials_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "serials.manage", "serials.html", "serials")


@router.get("/opac", response_class=HTMLResponse)
def opac_page(request: Request, settings: Settings = Depends(get_settings)):
    """The public catalogue search: no sign-in."""
    if not settings.opac_enabled:
        return HTMLResponse("Not found", status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "opac.html", {"app_name": settings.app_name, "version": __version__, "asset_v": asset_version()}
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "audit.read", "audit.html", "audit")


@router.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    me = principal.user.username if principal else ""
    return _gated_page(request, settings, principal, "admin.users", "admin.html", "admin", me=me)


@router.get("/updates", response_class=HTMLResponse)
def updates_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "admin.config", "updates.html", "updates")


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(request, settings, principal, "reports.view", "reports.html", "reports")


@router.get("/gate", response_class=HTMLResponse)
def gate_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    perms = principal.permissions if principal else frozenset()
    return _gated_page(
        request,
        settings,
        principal,
        "gate.view",
        "gate.html",
        "gate",
        simulator=settings.environment != "production" and "rfid.tag" in perms,
        can_admin="admin.config" in perms,
    )


@router.get("/inventory", response_class=HTMLResponse)
def inventory_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    return _gated_page(
        request,
        settings,
        principal,
        "inventory.run",
        "inventory.html",
        "inventory",
        simulator=settings.environment != "production",
    )


@router.get("/tags", response_class=HTMLResponse)
def tags_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
):
    perms = principal.permissions if principal else frozenset()
    return _gated_page(
        request, settings, principal, "catalogue.read", "tags.html", "registry", can_tag="rfid.tag" in perms
    )
