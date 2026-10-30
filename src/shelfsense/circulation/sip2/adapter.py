"""The SIP2 connector: the application's `CirculationConnector` spoken over the SIP2 wire protocol.

The ILMS owns the loan rules and the loans. This class sends what the application knows (member number, item number),
reads the answer, and gives back the protocol-neutral result the rest of the application already understands.

Retries without double counting. SIP2 has no transaction id, so a retry after a lost answer could lend a book twice or
renew it twice. The connector therefore remembers its answers by the application's transaction id (a repeat of a
finished request gets the same answer, `replayed=True`) and, when a request was sent but no answer arrived, checks the
ILMS before sending it again: is the book now charged to this member (checkout), now back (checkin), or does it have a
new due date (renew)? If so the earlier attempt succeeded and nothing is repeated.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

from shelfsense.circulation.connector import (
    CirculationConfig,
    ConnectorError,
    ConnectorResult,
    ItemView,
    MemberView,
)
from shelfsense.circulation.sip2 import codec
from shelfsense.circulation.sip2.client import Sip2Client, Sip2Unsure

log = logging.getLogger("shelfsense.sip2")

REMEMBER = 2000  # answers kept for repeat requests
BLANK_DATE = " " * codec.DATE_WIDTH
SUMMARY_CHARGED = "  Y       "  # patron information: list the items out on loan (position 3 of 10)

WORDING = {
    "ok": "Transaction OK",
    "patron_blocked": "Patron blocked",
    "fines_exceeded": "Patron has excessive fees",
    "item_on_loan": "Item already checked out",
    "not_on_loan": "Item not checked out",
    "loan_limit": "Too many items charged",
    "renewal_limit": "Renewal not allowed",
    "ilms_refused": "The ILMS refused the request",
}

# words an ILMS uses in its screen message, checked in this order for each kind of refusal
_PATRON = ("block", "privilege", "barred", "suspend", "expired", "invalid patron", "not allowed", "lost card")
_FINES = ("fine", "fee", "owe", "outstanding")
_ON_LOAN = ("already", "checked out", "charged to", "on loan")
_LIMIT = ("too many", "limit", "maximum")
_NOT_OUT = ("not checked out", "not charged", "not on loan", "not loaned")


def _has(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


class Sip2Connector:
    protocol = "sip2"

    def __init__(self, config: CirculationConfig | None = None, *, client: Sip2Client | None = None, **_o: object):
        self.config = config or CirculationConfig()
        self.client = client or Sip2Client(
            self.config.ilms_endpoint,
            self.config.ilms_port,
            login_user=self.config.ilms_login,
            login_password=self.config.ilms_password,
            location=self.config.ilms_location,
            institution=self.config.ilms_institution,
            timeout_s=self.config.ilms_timeout_s,
            checksum=self.config.ilms_checksum,
        )
        self._answers: OrderedDict[str, ConnectorResult] = OrderedDict()
        self._unsure: OrderedDict[str, None] = OrderedDict()  # ids whose request was sent without a usable answer
        self._lock = threading.RLock()

    def reconfigure(self, config: CirculationConfig) -> None:
        """Loan rules belong to the ILMS, so a change of the local rules needs nothing here."""

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ message building
    def _fields(self, *pairs: tuple[str, str]) -> list[tuple[str, str]]:
        base = [("AO", self.config.ilms_institution)]
        return base + list(pairs) + [("AC", "")]

    def _ask(self, code: str, fixed: str, fields: list[tuple[str, str]], expect: str) -> codec.Message:
        return self.client.request(code, fixed, fields, expect=expect)

    @staticmethod
    def _result(ok: bool, code: str, message: str = "", due: datetime | None = None, replayed: bool = False):
        return ConnectorResult(ok, code, message or WORDING[code], "sip2", due, replayed)

    # ------------------------------------------------------------------ the shared "remember and verify" wrapper
    def _run(self, txn_id: str, work) -> ConnectorResult:
        with self._lock:
            known = self._answers.get(txn_id)
            if known is not None:
                self._answers.move_to_end(txn_id)
                return ConnectorResult(known.ok, known.code, known.message, known.protocol, known.due_at, True)
            retry = txn_id in self._unsure
            try:
                result = work(retry)
            except Sip2Unsure:
                self._unsure[txn_id] = None
                while len(self._unsure) > REMEMBER:
                    self._unsure.popitem(last=False)  # the oldest are long past any retry
                raise
            self._unsure.pop(txn_id, None)
            self._answers[txn_id] = result
            while len(self._answers) > REMEMBER:
                self._answers.popitem(last=False)
            return result

    # ------------------------------------------------------------------ questions to the ILMS
    def _patron_flags(self, member_no: str) -> tuple[bool, str]:
        """(valid patron, the 14 patron status characters) from a patron status request."""
        reply = self._ask("23", "001" + codec.stamp(), self._fields(("AA", member_no)), "24")
        valid = reply.get("BL", "Y").upper() != "N"
        return valid, reply.fixed[:14].ljust(14)

    def _item_info(self, item_no: str) -> tuple[str, datetime | None]:
        """(circulation status code, due date) from an item information request."""
        reply = self._ask("17", codec.stamp(), self._fields(("AB", item_no)), "18")
        return reply.fixed[:2], codec.parse_date(reply.get("AH"))

    def _charged_items(self, member_no: str) -> list[str]:
        reply = self._ask("63", "001" + codec.stamp() + SUMMARY_CHARGED, self._fields(("AA", member_no)), "64")
        return reply.all("AU")

    def _reason_from_patron(self, member_no: str) -> str | None:
        """Ask the ILMS why this member may not borrow, from the patron status flags."""
        try:
            valid, flags = self._patron_flags(member_no)
        except ConnectorError:
            return None
        if not valid or flags[0] == "Y" or flags[4] == "Y":  # not valid, charge privileges denied, card reported lost
            return "patron_blocked"
        if flags[10] == "Y" or flags[11] == "Y":  # excessive outstanding fines or fees
            return "fines_exceeded"
        if flags[5] == "Y":  # too many items charged
            return "loan_limit"
        if flags[7] == "Y":  # too many renewals
            return "renewal_limit"
        return None

    def _refusal(self, op: str, reply: codec.Message, member_no: str = "") -> ConnectorResult:
        text = (reply.get("AF") + " " + reply.get("AG")).strip()
        code = ""
        if op == "checkin":
            code = "not_on_loan" if _has(text, _NOT_OUT) or not text else ""
        elif _has(text, _NOT_OUT):
            code = "not_on_loan"
        elif op == "renew" and _has(text, ("renew",) + _LIMIT):
            code = "renewal_limit"
        elif _has(text, _FINES):
            code = "fines_exceeded"
        elif _has(text, _PATRON):
            code = "patron_blocked"
        elif _has(text, _ON_LOAN):
            code = "item_on_loan"
        elif _has(text, _LIMIT):
            code = "loan_limit"
        if not code and member_no:
            code = self._reason_from_patron(member_no) or ""
        code = code or "ilms_refused"
        return self._result(False, code, text)

    # ------------------------------------------------------------------ CirculationConnector
    def patron_status(self, txn_id: str, member: MemberView) -> ConnectorResult:
        def work(retry: bool) -> ConnectorResult:
            valid, flags = self._patron_flags(member.member_no)
            if not valid or flags[0] == "Y" or flags[4] == "Y":
                return self._result(False, "patron_blocked")
            if flags[10] == "Y" or flags[11] == "Y":
                return self._result(False, "fines_exceeded")
            return self._result(True, "ok")

        return self._run(txn_id, work)

    def checkout(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult:
        def work(retry: bool) -> ConnectorResult:
            if retry and item.accession_no in self._charged_items(member.member_no):
                return self._after_success(item.accession_no, None, now, replayed=True)  # the first try went through
            reply = self._ask(
                "11",
                "NN" + codec.stamp(now) + BLANK_DATE,
                self._fields(("AA", member.member_no), ("AB", item.accession_no)),
                "12",
            )
            if codec.flag(reply.fixed[:1]):
                return self._after_success(item.accession_no, codec.parse_date(reply.get("AH")), now)
            return self._refusal("checkout", reply, member.member_no)

        return self._run(txn_id, work)

    def renew(self, txn_id: str, member: MemberView, item: ItemView, *, now: datetime) -> ConnectorResult:
        def work(retry: bool) -> ConnectorResult:
            if retry:  # did the first try go through? then the ILMS shows a different due date than we had
                _, due = self._item_info(item.accession_no)
                if due is not None and item.due_at is not None and due != item.due_at:
                    return self._result(True, "ok", due=due, replayed=True)
            reply = self._ask(
                "29",
                "NN" + codec.stamp(now) + BLANK_DATE,
                self._fields(("AA", member.member_no), ("AB", item.accession_no)),
                "30",
            )
            if codec.flag(reply.fixed[:1]):
                return self._after_success(item.accession_no, codec.parse_date(reply.get("AH")), now)
            return self._refusal("renew", reply, member.member_no)

        return self._run(txn_id, work)

    def checkin(self, txn_id: str, item: ItemView, *, now: datetime) -> ConnectorResult:
        def work(retry: bool) -> ConnectorResult:
            fields = [("AP", self.config.ilms_location)] + self._fields(("AB", item.accession_no))
            reply = self._ask("09", "N" + codec.stamp(now) + codec.stamp(now), fields, "10")
            if codec.flag(reply.fixed[:1]):
                return self._result(True, "ok")
            if retry:  # the first try may have worked: the book is no longer charged
                status, _ = self._item_info(item.accession_no)
                if status != "04":
                    return self._result(True, "ok", replayed=True)
            return self._refusal("checkin", reply)

        return self._run(txn_id, work)

    def due_date(self, txn_id: str, item: ItemView) -> ConnectorResult:
        def work(retry: bool) -> ConnectorResult:
            status, due = self._item_info(item.accession_no)
            if status == "04":  # charged
                return self._result(True, "ok", due=due)
            return self._result(False, "not_on_loan")

        return self._run(txn_id, work)

    # ------------------------------------------------------------------ helpers
    def _after_success(
        self, item_no: str, due: datetime | None, now: datetime, *, replayed: bool = False
    ) -> ConnectorResult:
        """A loan or renewal went through. Some ILMS leave the due date out of the answer: ask for it."""
        if due is None:
            try:
                _, due = self._item_info(item_no)
            except ConnectorError:
                due = None
        if due is None:
            due = now + timedelta(days=self.config.loan_days)  # the local rule, only when the ILMS gave none
        return self._result(True, "ok", due=due, replayed=replayed)

    def status(self) -> codec.Message:
        """The ILMS's own status answer (online or not, its timeout), used by `shelfsense doctor`."""
        return self._ask("99", "0" + "080" + "2.00", [], "98")
