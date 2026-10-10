"""The background loop: look for reminders that are due, then deliver whatever is queued, every N seconds.

`run_once` is one pass (the CLI and the loop both call it). `Loop` runs passes on a daemon thread while the
application is up; it survives any error in a pass (logged, tried again next time), does nothing while a restore
is running, and stops promptly when the application shuts down.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from aisys.notifications import queue, reminders
from aisys.notifications.providers import NotificationConfig, NotificationProvider

log = logging.getLogger("aisys.notifications")
BATCH = 100  # what one dispatch call takes
MAX_ROUNDS = 100  # at most this many calls per pass (10,000 messages), so one pass cannot run for ever


def run_once(
    factory: sessionmaker[Session],
    providers: dict[str, NotificationProvider],
    config: NotificationConfig,
    *,
    library_name: str = "The library",
    now: datetime | None = None,
) -> dict:
    """One pass: queue due reminders, then try every queued message that is due."""
    with factory() as db:
        scanned = reminders.scan(db, config, library_name=library_name, now=now)
        delivered = queue.dispatch(db, providers, config, now=now)
        for _ in range(
            MAX_ROUNDS - 1
        ):  # a big batch (a whole day's reminders) is delivered in this pass, not 100 a minute
            if delivered["tried"] < BATCH:
                break
            more = queue.dispatch(db, providers, config, now=now)
            for key in ("tried", "sent", "retry_later", "failed", "cancelled"):
                delivered[key] += more[key]
            delivered["waiting"], delivered["next_attempt_at"] = more["waiting"], more["next_attempt_at"]
    return {"reminders": scanned, "delivery": delivered}


class Loop:
    def __init__(
        self,
        pass_fn: Callable[[], dict],
        every_s: float,
        *,
        paused: Callable[[], bool] = lambda: False,
        name: str = "aisys-notify",
    ):
        self.pass_fn, self.every_s, self.paused = pass_fn, every_s, paused
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.passes = 0
        self.errors = 0
        self.last: dict | None = None

    def start(self) -> Loop:
        self.thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout)

    def run_pass(self) -> None:
        if self.paused():  # a restore is replacing the data: touch nothing
            return
        try:
            self.last = self.pass_fn()
            self.passes += 1
            sent = (self.last.get("delivery") or {}).get("sent", 0)
            if sent or (self.last.get("reminders") or {}).get("queued") or self.last.get("events"):
                log.info("%s pass: %s", self.thread.name, self.last)
        except Exception:  # noqa: BLE001 - the loop must outlive any single bad pass
            self.errors += 1
            log.exception("%s pass failed; will try again in %s s", self.thread.name, self.every_s)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.run_pass()
            self.stop_event.wait(self.every_s)
