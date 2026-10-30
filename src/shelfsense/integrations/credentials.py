"""Smart-card credential provider boundary.

A real provider would talk to a card reader / PKI (challenge-response) and return the *verified*
card UID. The application only ever consumes `CardPresentation` -> verified uid, so swapping the mock
for a vendor SDK does not touch the auth logic. Whether a UID belongs to staff or a patron is decided
by the application's own card registry (the `cards` table), never by the provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class CredentialError(Exception):
    """Card could not be read or verified."""


@dataclass(frozen=True)
class CardPresentation:
    """What a reader hands us. `proof` is where a real card's signature/response would travel."""

    uid: str
    proof: str | None = None


class CredentialProvider(Protocol):
    def verify(self, presentation: CardPresentation) -> str:
        """Return the verified card UID or raise CredentialError."""


_UID = re.compile(r"^[A-Z0-9]{6,32}$")


class MockCredentialProvider:
    """Accepts any well-formed UID. Stands in for a vendor SDK that is unavailable."""

    def verify(self, presentation: CardPresentation) -> str:
        uid = presentation.uid.strip().upper()
        if not _UID.match(uid):
            raise CredentialError("malformed card UID")
        return uid
