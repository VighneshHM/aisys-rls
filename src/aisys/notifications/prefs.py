"""Per-user notification preferences: which channels a staff user wants alerts on, and where to reach them."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aisys import audit
from aisys.models import NotificationPref, User
from aisys.notifications.providers import CHANNELS, address_hint, valid_address


class PrefError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def recipients(db: Session) -> list[tuple[User, str, str]]:
    """(user, channel, address) for every active preference of an active user."""
    rows = db.execute(
        select(User, NotificationPref.channel, NotificationPref.address)
        .join(NotificationPref, NotificationPref.user_id == User.id)
        .where(User.active, NotificationPref.active)
        .order_by(User.username, NotificationPref.channel)
    ).all()
    return [(u, c, a) for u, c, a in rows]


def list_prefs(db: Session) -> list[dict]:
    """Every active staff user with their three channels (switched off and empty if never set)."""
    users = db.scalars(select(User).where(User.active).order_by(User.username)).all()
    prefs: dict[tuple[int, str], NotificationPref] = {
        (p.user_id, p.channel): p for p in db.scalars(select(NotificationPref))
    }
    out = []
    for u in users:
        channels = {}
        for ch in CHANNELS:
            p = prefs.get((u.id, ch))
            channels[ch] = {"active": bool(p and p.active), "address": p.address if p else ""}
        out.append({"user_id": u.id, "username": u.username, "display_name": u.display_name, "channels": channels})
    return out


def set_prefs(db: Session, user_id: int, channels: dict[str, dict], *, actor: str, ip: str = "") -> dict:
    """Replace a user's settings for the channels given. Turning a channel on needs a valid address."""
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise PrefError(404, "No such staff user")
    unknown = set(channels) - set(CHANNELS)
    if unknown:
        raise PrefError(422, f"Unknown channel: {', '.join(sorted(unknown))}")
    existing = {p.channel: p for p in db.scalars(select(NotificationPref).where(NotificationPref.user_id == user_id))}
    for channel, setting in channels.items():
        address = (setting.get("address") or "").strip()
        active = bool(setting.get("active"))
        if active and not valid_address(channel, address):
            raise PrefError(422, f"For {channel}, give {address_hint(channel)}")
        if address and not valid_address(channel, address):
            raise PrefError(422, f"For {channel}, give {address_hint(channel)}")
        row = existing.get(channel)
        if row is None:
            if not address:
                continue  # nothing to store for an unused channel
            row = NotificationPref(user_id=user_id, channel=channel, address=address, active=active)
            db.add(row)
        else:
            row.address, row.active = address or row.address, active
    audit.record(
        db,
        actor=actor,
        action="notification.prefs",
        entity="user",
        entity_id=user.username,
        detail={"channels": {c: bool(s.get("active")) for c, s in channels.items()}},
        ip=ip,
    )
    db.commit()
    return next(p for p in list_prefs(db) if p["user_id"] == user_id)
