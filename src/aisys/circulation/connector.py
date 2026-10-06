"""The ILMS connector boundary (SOP section 6, interoperability layer).

The application never talks to an ILMS directly. It calls a `CirculationConnector`; a real SIP2 or NCIP 2.0 adapter is
a class that implements this Protocol and is registered under a driver name (see `registry.py`). Results use
protocol-neutral codes plus a human message, the way both SIP2 ("screen message") and NCIP ("problem") report them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class ConnectorError(Exception):
    """The ILMS could not be reached or answered with a transport-level failure. Safe to retry."""


class ConnectorTimeout(ConnectorError):  # noqa: N818 - reads better at call sites
    """The ILMS did not answer in time. The request may or may not have been processed: retry with the same txn_id."""


@dataclass(frozen=True)
class CirculationConfig:
    """Library policy and connector settings (config file sections [circulation] and [ilms])."""

    fine_limit_cents: int = 500  # a member whose fines EXCEED this cannot borrow
    loan_days: int = 14
    max_renewals: int = 2
    max_loans: int = 5
    ilms_driver: str = "mock"
    ilms_protocol: str = "sip2"  # which dialect the mock imitates in its messages: sip2 | ncip
    ilms_retries: int = 2  # extra attempts after the first
    ilms_backoff_s: float = 0.1


@dataclass(frozen=True)
class MemberView:
    """What the ILMS needs to know about the borrower (the local record is a cache of the ILMS's)."""

    member_no: str
    status: str  # active | blocked
    fines_cents: int
    open_loans: int
    block_reason: str = ""


@dataclass(frozen=True)
class ItemView:
    accession_no: str
    on_loan: bool
    renewals: int = 0
    due_at: datetime | None = None


@dataclass(frozen=True)
class ConnectorResult:
    ok: bool
    # ok | patron_blocked | fines_exceeded | item_on_loan | not_on_loan | loan_limit | renewal_limit | ilms_unavailable
    code: str
    message: str
    protocol: str = ""
    due_at: datetime | None = None
    replayed: bool = False  # True when the ILMS recognised the txn_id and repeated its earlier answer


@runtime_checkable
class CirculationConnector(Protocol):
    protocol: str

    def patron_status(self, txn_id: str, member: MemberView) -> ConnectorResult: ...
    def checkout(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult: ...
    def renew(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult: ...
    def checkin(self, txn_id: str, item: ItemView, *, now: datetime) -> ConnectorResult: ...
    def due_date(self, txn_id: str, item: ItemView) -> ConnectorResult: ...
