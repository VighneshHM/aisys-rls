"""Mock ILMS: stands in for the institution's real circulation system (out of scope).

It owns the *loan policy* (loan period, renewal and loan limits) and answers in SIP2 wording. It is stateless
about loans, apart from remembering answers by transaction id so a retried request is never processed twice. Failure
injection (`fail_next`) lets tests and demos exercise timeouts, outages and lost replies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock

from shelfsense.circulation.connector import (
    CirculationConfig,
    ConnectorError,
    ConnectorResult,
    ConnectorTimeout,
    ItemView,
    MemberView,
)

# each outcome, worded the way a SIP2 ILMS reports it (the only protocol the add-on speaks; see docs/decisions.md DR-13)
WORDING = {
    "ok": "Transaction OK",
    "patron_blocked": "Patron blocked",
    "fines_exceeded": "Patron has excessive fees",
    "item_on_loan": "Item already checked out",
    "not_on_loan": "Item not checked out",
    "loan_limit": "Too many items charged",
    "renewal_limit": "Renewal not allowed: maximum renewals reached",
}


class MockIlms:
    def reconfigure(self, config: CirculationConfig) -> None:
        """Take new loan and fine rules without losing what the mock already holds (a real ILMS owns its own rules)."""
        self.config = config

    def __init__(self, config: CirculationConfig | None = None, **_options: object) -> None:
        self.config = config or CirculationConfig()
        self.protocol = "sip2"
        self.calls: list[tuple[str, str]] = []  # (operation, txn_id) for every request received
        self._answers: dict[str, ConnectorResult] = {}
        self._injected: list[str] = []
        self._lock = RLock()

    # ---- simulator controls ---------------------------------------------------------------------
    def fail_next(self, mode: str = "timeout", times: int = 1) -> None:
        """Make the next `times` requests fail. timeout / error: never processed. lost_reply: processed, reply lost."""
        if mode not in ("timeout", "error", "lost_reply"):
            raise ValueError(f"unknown failure mode '{mode}'")
        self._injected.extend([mode] * times)

    def pending_failures(self) -> int:
        return len(self._injected)

    def clear_failures(self) -> None:
        self._injected.clear()

    # ---- helpers --------------------------------------------------------------------------------
    def _result(self, ok: bool, code: str, due_at: datetime | None = None) -> ConnectorResult:
        return ConnectorResult(ok, code, WORDING[code], self.protocol, due_at)

    def _run(self, op: str, txn_id: str, decide) -> ConnectorResult:
        with self._lock:
            self.calls.append((op, txn_id))
            mode = self._injected.pop(0) if self._injected else ""
            if mode == "timeout":
                raise ConnectorTimeout("simulated ILMS timeout")
            if mode == "error":
                raise ConnectorError("simulated ILMS outage")
            if txn_id in self._answers:  # already processed: answer again, do not repeat
                first = self._answers[txn_id]
                answer = ConnectorResult(first.ok, first.code, first.message, first.protocol, first.due_at, True)
            else:
                answer = self._answers[txn_id] = decide()
            if mode == "lost_reply":
                raise ConnectorTimeout("simulated lost reply (the request WAS processed)")
            return answer

    def _can_borrow(self, member: MemberView) -> ConnectorResult | None:
        if member.status != "active":
            return self._result(False, "patron_blocked")
        if member.fines_cents > self.config.fine_limit_cents:
            return self._result(False, "fines_exceeded")
        return None

    # ---- CirculationConnector -------------------------------------------------------------------
    def patron_status(self, txn_id: str, member: MemberView) -> ConnectorResult:
        return self._run("patron_status", txn_id, lambda: self._can_borrow(member) or self._result(True, "ok"))

    def checkout(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult:
        def decide() -> ConnectorResult:
            if (refusal := self._can_borrow(member)) is not None:
                return refusal
            if item.on_loan:
                return self._result(False, "item_on_loan")
            if member.open_loans >= self.config.max_loans:
                return self._result(False, "loan_limit")
            return self._result(True, "ok", now + timedelta(days=self.config.loan_days))

        return self._run("checkout", txn_id, decide)

    def renew(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult:
        def decide() -> ConnectorResult:
            if not item.on_loan:
                return self._result(False, "not_on_loan")
            if (refusal := self._can_borrow(member)) is not None:
                return refusal
            if item.renewals >= self.config.max_renewals:
                return self._result(False, "renewal_limit")
            return self._result(True, "ok", now + timedelta(days=self.config.loan_days))

        return self._run("renew", txn_id, decide)

    def checkin(self, txn_id: str, item: ItemView, *, now: datetime) -> ConnectorResult:
        return self._run(
            "checkin", txn_id, lambda: self._result(True, "ok") if item.on_loan else self._result(False, "not_on_loan")
        )

    def due_date(self, txn_id: str, item: ItemView) -> ConnectorResult:
        return self._run(
            "due_date",
            txn_id,
            lambda: self._result(True, "ok", item.due_at) if item.on_loan else self._result(False, "not_on_loan"),
        )
