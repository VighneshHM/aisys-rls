"""Disaster drill helpers (development only): a deliberately damaging change, to practise restoring from a backup.

`faulty_sync` imitates a misbehaving integration that overwrites data it should not touch. It is only reachable through
the simulator endpoints, which answer 404 in production.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session


def faulty_sync(db: Session) -> dict:
    """What a bad sync could do: forget fines and blocks, blank shelf locations, drop tag links, close loans."""
    changed = {}
    changed["members: balances wiped and blocks lifted"] = cast(
        "CursorResult[Any]",
        db.execute(text("UPDATE members SET fines_cents = 0, status = 'active', block_reason = ''")),
    ).rowcount
    changed["items: shelf location blanked on every second item"] = cast(
        "CursorResult[Any]", db.execute(text("UPDATE items SET location = '' WHERE id % 2 = 0"))
    ).rowcount
    changed["rfid_tags: every fifth tag link deleted"] = cast(
        "CursorResult[Any]", db.execute(text("DELETE FROM rfid_tags WHERE id % 5 = 0"))
    ).rowcount
    changed["loans: every open loan closed"] = cast(
        "CursorResult[Any]",
        db.execute(
            text(
                "UPDATE loans SET returned_at = CURRENT_TIMESTAMP, returned_by = 'faulty-sync' "
                "WHERE returned_at IS NULL"
            )
        ),
    ).rowcount
    db.commit()
    return {"changes": [{"what": k, "rows": v} for k, v in changed.items()], "rows_touched": sum(changed.values())}
