"""Library rules an administrator may change in the browser (FR11), on top of the TOML file.

Only values that are safe to change while the system runs are listed here: loan and fine rules, reminders, the gate's
duplicate window, sign-in limits and the public catalogue switch. Everything else (database, devices, drivers,
certificates, secrets) stays in the TOML file and the environment, because changing it needs a restart and a person
at the server. A saved value is stored in `app_settings` and wins over the file; removing it goes back to the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from aisys.config import Settings


@dataclass(frozen=True)
class Spec:
    key: str  # "circulation.loan_days"
    group: str
    label: str
    help: str
    kind: str  # "int" or "bool"
    low: int = 0
    high: int = 0
    unit: str = ""


SPECS: tuple[Spec, ...] = (
    Spec(
        "circulation.loan_days", "Loans", "Loan period", "How long a new loan or renewal lasts.", "int", 1, 365, "days"
    ),
    Spec(
        "circulation.max_renewals", "Loans", "Renewals allowed", "How many times one loan can be renewed.", "int", 0, 20
    ),
    Spec(
        "circulation.max_loans", "Loans", "Items per member", "The most items one member may have out.", "int", 1, 100
    ),
    Spec(
        "circulation.fine_limit_cents",
        "Fines",
        "Block above",
        "A member owing more than this cannot borrow.",
        "int",
        0,
        1_000_000,
        "cents",
    ),
    Spec(
        "circulation.fine_per_day_cents",
        "Fines",
        "Fine per day",
        "Charged for each late day.",
        "int",
        0,
        100_000,
        "cents",
    ),
    Spec("circulation.grace_days", "Fines", "Grace period", "Late days that cost nothing.", "int", 0, 60, "days"),
    Spec(
        "circulation.max_fine_per_loan_cents",
        "Fines",
        "Cap per loan",
        "The most one late loan can cost (0 for no cap).",
        "int",
        0,
        1_000_000,
        "cents",
    ),
    Spec(
        "notifications.reminders",
        "Reminders",
        "Send loan reminders",
        "Remind members before and on the due day.",
        "bool",
    ),
    Spec(
        "notifications.remind_days_before",
        "Reminders",
        "Remind before due",
        "How many days before the due date.",
        "int",
        1,
        30,
        "days",
    ),
    Spec(
        "notifications.max_attempts",
        "Reminders",
        "Delivery attempts",
        "Tries before a message is marked failed.",
        "int",
        1,
        20,
    ),
    Spec(
        "gate.duplicate_window_s",
        "Gate",
        "Duplicate window",
        "Ignore the same tag seen again within this time.",
        "int",
        0,
        3600,
        "seconds",
    ),
    Spec(
        "security.session_ttl_minutes",
        "Sign-in",
        "Session length",
        "How long a sign-in lasts (applies to new sign-ins).",
        "int",
        5,
        10_080,
        "minutes",
    ),
    Spec(
        "security.max_failed_logins",
        "Sign-in",
        "Failed attempts before lock",
        "Wrong passwords before an account locks.",
        "int",
        1,
        20,
    ),
    Spec(
        "security.lockout_minutes",
        "Sign-in",
        "Lock time",
        "How long a locked account stays locked.",
        "int",
        1,
        1440,
        "minutes",
    ),
    Spec(
        "opac.enabled",
        "Public catalogue",
        "Public catalogue on",
        "Let anyone search the catalogue without signing in.",
        "bool",
    ),
)
BY_KEY = {s.key: s for s in SPECS}

# where each key lives in the Settings object
_TOP = {
    "security.session_ttl_minutes": "session_ttl_minutes",
    "security.max_failed_logins": "max_failed_logins",
    "security.lockout_minutes": "lockout_minutes",
    "opac.enabled": "opac_enabled",
}


class SettingError(ValueError):
    pass


def current(settings: Settings, key: str):
    if key in _TOP:
        return getattr(settings, _TOP[key])
    section, name = key.split(".")
    return getattr(getattr(settings, section), name)


def parse(spec: Spec, raw) -> int | bool:
    """A value of the right type and size, or a SettingError that says what is wrong in plain words."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "on", "yes"):
            return True
        if str(raw).lower() in ("false", "0", "off", "no"):
            return False
        raise SettingError(f"{spec.label}: use on or off.")
    whole = f"{spec.label}: use a whole number."
    if isinstance(raw, bool):
        raise SettingError(whole)
    if isinstance(raw, float):
        if not raw.is_integer():  # also false for NaN and infinity
            raise SettingError(whole)
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"-?[0-9]+", raw.strip()):  # plain digits only: not 1_0 or other scripts
        try:
            value = int(raw.strip())
        except ValueError:  # thousands of digits
            raise SettingError(whole) from None
    else:
        raise SettingError(whole)
    if not spec.low <= value <= spec.high:
        raise SettingError(f"{spec.label}: use a number from {spec.low} to {spec.high}.")
    return value


def store(value: int | bool) -> str:
    return "true" if value is True else "false" if value is False else str(int(value))


def apply(settings: Settings, values: dict[str, int | bool]) -> Settings:
    """A copy of `settings` with these values in place."""
    top: dict = {}
    nested: dict[str, dict] = {}
    for key, value in values.items():
        if key in _TOP:
            top[_TOP[key]] = value
        else:
            section, name = key.split(".")
            nested.setdefault(section, {})[name] = value
    for section, changes in nested.items():
        top[section] = replace(getattr(settings, section), **changes)
    return replace(settings, **top)


def overrides(rows) -> dict[str, int | bool]:
    """Saved rows as values; a row that is no longer valid is ignored, so a bad row cannot stop startup."""
    out: dict[str, int | bool] = {}
    for row in rows:
        spec = BY_KEY.get(row.key)
        if spec is None:
            continue
        try:
            out[row.key] = parse(spec, row.value)
        except SettingError:
            continue
    return out
