"""Full-text search over the catalogue (SQLite FTS5), kept current by database triggers (migration 0010).

`match_query` turns what a person typed into a safe FTS5 query: every word must match, and the last word may be
unfinished ("dispos" finds "The Dispossessed"), so results appear while typing. Nothing typed is ever passed to FTS5
as syntax, so quotes, `OR`, `NEAR`, `*` and the like are just words.
"""

from __future__ import annotations

import re

from sqlalchemy import Float, Integer, text
from sqlalchemy.orm import Session

MAX_WORDS = 8
MAX_WORD_LEN = 64
_WORD = re.compile(r"\w+", re.UNICODE)


def available(db: Session) -> bool:
    """True when this database has the full-text index (SQLite built with FTS5 and migration 0010 applied)."""
    return bool(db.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_fts'")).first())


def match_query(q: str) -> str:
    """The FTS5 expression for a typed search, or '' when there is nothing searchable in it."""
    words = [w[:MAX_WORD_LEN] for w in _WORD.findall(q)][:MAX_WORDS]
    return " ".join(f'"{w}"*' for w in words)


def has_hits(db: Session, match: str) -> bool:
    """Does the index find anything for this query? (A cheap check that decides how the rest of the search runs.)"""
    return bool(db.execute(text("SELECT 1 FROM item_fts WHERE item_fts MATCH :m LIMIT 1"), {"m": match}).first())


def ranked_subquery(match: str):
    """A subquery of (id, rank): the items matching, best first (bm25: smaller is better)."""
    return (
        text(
            "SELECT rowid AS id, bm25(item_fts, 8.0, 4.0, 3.0, 1.0, 6.0, 6.0, 2.0, 1.0) AS rank "
            "FROM item_fts WHERE item_fts MATCH :m"
        )
        .bindparams(m=match)
        .columns(id=Integer, rank=Float)
        .subquery("ft")
    )
