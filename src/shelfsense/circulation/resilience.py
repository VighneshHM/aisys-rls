"""Retry wrapper: every connector call gets bounded retries with the SAME transaction id.

Re-using the id is what makes a retry safe: if the first attempt reached the ILMS but its reply was lost, the ILMS
recognises the id and repeats its answer instead of lending the book twice. When the attempts run out the caller gets
an ordinary `ilms_unavailable` result and decides what to do (we change nothing locally).
"""

from __future__ import annotations

import logging
import time

from shelfsense.circulation.connector import CirculationConnector, ConnectorError, ConnectorResult

log = logging.getLogger("shelfsense.circulation")


class ResilientConnector:
    def __init__(self, inner: CirculationConnector, *, retries: int = 2, backoff_s: float = 0.1) -> None:
        self.inner = inner
        self.retries = max(0, retries)
        self.backoff_s = backoff_s
        self.protocol = inner.protocol

    def reconfigure(self, config) -> None:
        if hasattr(self.inner, "reconfigure"):
            self.inner.reconfigure(config)

    def _call(self, op: str, txn_id: str, *args, **kwargs) -> ConnectorResult:
        last = ""
        for attempt in range(self.retries + 1):
            try:
                return getattr(self.inner, op)(txn_id, *args, **kwargs)
            except ConnectorError as exc:
                last = str(exc)
                log.warning("ILMS %s failed (attempt %d): %s", op, attempt + 1, exc, extra={"ctx": {"txn_id": txn_id}})
                if attempt < self.retries and self.backoff_s:
                    time.sleep(self.backoff_s * (attempt + 1))
        message = f"The library system did not answer after {self.retries + 1} tries ({last})"
        return ConnectorResult(False, "ilms_unavailable", message, self.protocol)

    def patron_status(self, txn_id, member):
        return self._call("patron_status", txn_id, member)

    def checkout(self, txn_id, member, item, *, now):
        return self._call("checkout", txn_id, member, item, now=now)

    def renew(self, txn_id, member, item, *, now):
        return self._call("renew", txn_id, member, item, now=now)

    def checkin(self, txn_id, item, *, now):
        return self._call("checkin", txn_id, item, now=now)

    def due_date(self, txn_id, item):
        return self._call("due_date", txn_id, item)
