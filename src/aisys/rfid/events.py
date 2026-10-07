"""Device-neutral events emitted by readers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

TAG_UID_RE = re.compile(r"^[0-9A-F]{8,32}$")


def normalize_tag_uid(raw: str) -> str:
    """Tag UIDs/EPCs are hex; readers differ in case and separators, so store one canonical form."""
    return re.sub(r"[\s:-]", "", raw or "").upper()


def is_valid_tag_uid(uid: str) -> bool:
    return bool(TAG_UID_RE.match(uid))


@dataclass(frozen=True)
class TagObserved:
    """A tag was seen in a reader's field. `item_ref` is the identifier written on the tag (accession no.)."""

    uid: str
    reader_id: str
    at: datetime
    item_ref: str | None = None
    security_armed: bool | None = None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class Passage:
    """One walk through a security gate: how many people, and every tag the gate read on them.

    `TagObserved.security_armed` is the security bit as the gate read it from the tag itself, which is what makes the
    alarm decision independent of the library system.
    """

    gate_id: str
    at: datetime
    people: int = 1
    tags: tuple[TagObserved, ...] = ()
