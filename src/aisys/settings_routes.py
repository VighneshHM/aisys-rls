"""HTTP API behind the Settings page (FR11): see how the system is set up and change the library's rules.

`admin.config` is needed for all of it. Only the values in `settings_admin.SPECS` can be changed; the rest of the
configuration is shown read-only (secrets never appear). A change is checked as a whole, saved, applied at once and
written to the audit log with the old and new value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys import audit, settings_admin
from aisys.config import Settings
from aisys.deps import Principal, get_db, get_settings, require
from aisys.models import AppSetting
from aisys.timeutil import iso

router = APIRouter(tags=["admin"])
CanConfig = Depends(require("admin.config"))


class ChangeIn(BaseModel):
    values: dict[str, Any]  # checked one by one in settings_admin.parse, which words the problem for people


def _fact(group: str, label: str, value: str) -> dict:
    return {"group": group, "label": label, "value": value}


def _readonly(s: Settings) -> list[dict]:
    """How the system is set up, as plain facts. Nothing secret: no keys, no passwords, no database path details."""
    db = s.database_url.rsplit("/", 1)[-1] if s.database_url.startswith("sqlite") else "an external database"
    devices = ", ".join(f"{d.id} ({d.kind}, {d.driver})" for d in s.devices)
    c, n = s.circulation, s.notifications
    ilms = f"{c.ilms_driver}, {c.ilms_protocol.upper()}"
    ilms += f", {c.ilms_endpoint}:{c.ilms_port}" if c.ilms_endpoint else ", no address set"
    return [
        _fact("System", "Library name", s.app_name),
        _fact("System", "Environment", s.environment),
        _fact("System", "Address", f"{s.host}:{s.port}" + (" (HTTPS)" if s.tls_certfile else " (HTTP)")),
        _fact("System", "Database", db),
        _fact("System", "Backup folder", s.backup_dir),
        _fact("System", "Log", f"{s.log_level}, {s.log_format}"),
        _fact("Connections", "Library system (ILMS)", ilms),
        _fact("Connections", "ILMS retries", str(c.ilms_retries)),
        _fact("Connections", "Net cataloguing", s.netcat_driver),
        _fact("Connections", "E-mail, SMS, print", f"{n.email_driver}, {n.sms_driver}, {n.print_driver}"),
        _fact("Connections", "Devices", devices),
        _fact("Connections", "Message loop", f"every {n.auto_dispatch_s:g} s" if n.auto_dispatch_s else "off"),
    ]


def _items(settings: Settings, base: Settings, rows: dict[str, AppSetting]) -> list[dict]:
    out = []
    for spec in settings_admin.SPECS:
        row = rows.get(spec.key)
        out.append(
            {
                "key": spec.key,
                "group": spec.group,
                "label": spec.label,
                "help": spec.help,
                "kind": spec.kind,
                "min": spec.low,
                "max": spec.high,
                "unit": spec.unit,
                "value": settings_admin.current(settings, spec.key),
                "file_value": settings_admin.current(base, spec.key),
                "changed": row is not None,
                "updated_by": row.updated_by if row else "",
                "updated_at": iso(row.updated_at) if row else None,
            }
        )
    return out


def _reload(request: Request, db: Session) -> Settings:
    """Make the saved values live: new settings for every request, and the ILMS stand-in gets the new loan rules."""
    base: Settings = request.app.state.base_settings
    new = settings_admin.apply(base, settings_admin.overrides(db.scalars(select(AppSetting))))
    request.app.state.settings = new
    reconfigure = getattr(request.app.state.ilms, "reconfigure", None)
    if reconfigure:
        reconfigure(new.circulation)
    return new


@router.get("/api/admin/settings")
def read_settings(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    _: Principal = CanConfig,
) -> dict:
    rows = {r.key: r for r in db.scalars(select(AppSetting))}
    return {
        "items": _items(settings, request.app.state.base_settings, rows),
        "readonly": _readonly(settings),
        "note": "Changes apply at once. Anything not listed here (devices, drivers, certificates, secrets) is set in "
        "the configuration file and needs a restart.",
    }


@router.put("/api/admin/settings")
def change_settings(
    body: ChangeIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = CanConfig,
) -> dict:
    if not body.values:
        raise HTTPException(422, {"message": "Nothing to change.", "problems": []})
    parsed: dict[str, int | bool] = {}
    problems = []
    for key, raw in body.values.items():
        spec = settings_admin.BY_KEY.get(key)
        if spec is None:
            problems.append({"field": key, "message": "This setting cannot be changed here."})
            continue
        try:
            parsed[key] = settings_admin.parse(spec, raw)
        except settings_admin.SettingError as exc:
            problems.append({"field": key, "message": str(exc)})
    if problems:  # all or nothing: one bad value saves none of them
        raise HTTPException(422, {"message": "Nothing was saved: " + problems[0]["message"], "problems": problems})
    before = request.app.state.settings
    now = datetime.now(UTC)
    changes = {}
    for key, value in parsed.items():
        old = settings_admin.current(before, key)
        if old == value:
            continue  # already the case: nothing to record
        row = db.get(AppSetting, key)
        text = settings_admin.store(value)
        if row is None:
            db.add(AppSetting(key=key, value=text, updated_by=principal.user.username, updated_at=now))
        else:
            row.value, row.updated_by, row.updated_at = text, principal.user.username, now
        changes[key] = {"from": old, "to": value}
    if changes:
        audit.record(
            db,
            actor=principal.user.username,
            action="admin.settings.update",
            entity="settings",
            entity_id="",
            detail={"changes": changes},
        )
    db.commit()
    _reload(request, db)
    return {"changed": list(changes), "message": f"Saved {len(changes)} change(s)." if changes else "Nothing changed."}


@router.delete("/api/admin/settings/{key}")
def reset_setting(
    key: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = CanConfig,
) -> dict:
    if key not in settings_admin.BY_KEY:
        raise HTTPException(404, {"message": "No such setting.", "problems": []})
    row = db.get(AppSetting, key)
    if row is None:
        raise HTTPException(
            409, {"message": "That setting already has the configuration file's value.", "problems": []}
        )
    old = settings_admin.current(request.app.state.settings, key)
    db.delete(row)
    audit.record(
        db,
        actor=principal.user.username,
        action="admin.settings.reset",
        entity="settings",
        entity_id=key,
        detail={"from": old},
    )
    db.commit()
    new = _reload(request, db)
    return {"key": key, "value": settings_admin.current(new, key)}
