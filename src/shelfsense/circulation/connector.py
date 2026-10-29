"""The ILMS connector boundary (interoperability layer).

The application never talks to an ILMS directly. It calls a `CirculationConnector`; the SIP2 adapter (`sip2/`) and the
mock are classes that implement this Protocol and are registered under a driver name (see `registry.py`). Results use
protocol-neutral codes plus a human message (SIP2's "screen message"), so another protocol, such as NCIP 2.0 if a site
ever needs it (not built, docs/decisions.md DR-13), would be one more class behind the same boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    fine_per_day_cents: int = 25  # charged for each billable day an item comes back late
    grace_days: int = 1  # whole days late that cost nothing
    max_fine_per_loan_cents: int = 1000  # one loan never costs more than this
    ilms_driver: str = "mock"
    ilms_endpoint: str = (
        ""  # host name or IP address of the ILMS server (a real connector uses it; the mock ignores it)
    )
    ilms_port: int = 0  # the SIP2 port of the ILMS server (0 = not set)
    ilms_login: str = ""  # SIP2 login user (CN); empty means the server needs no login
    ilms_password: str = field(default="", repr=False)  # SIP2 login password (CO): from SHELFSENSE_ILMS_PASSWORD only
    ilms_institution: str = ""  # SIP2 institution id (AO)
    ilms_location: str = ""  # SIP2 location code (CP/AP) of this system
    ilms_timeout_s: float = 5.0  # seconds to wait for an answer
    ilms_checksum: bool = True  # SIP2 error detection (sequence number and checksum)
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
