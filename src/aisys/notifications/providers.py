"""Provider-neutral notification adapters (SOP section 6, integration layer: email, SMS and print connectors).

The application never talks to an email, SMS or print service directly. It hands a message to a
`NotificationProvider`; a real one (an SMTP relay, an SMS gateway, a network printer) is a class implementing this
Protocol, registered with `register_provider` and chosen in the config. The mocks below stand in for them and can be
told to fail, so retries can be demonstrated and tested.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol, runtime_checkable

CHANNELS = ("email", "sms", "print")
ADDRESS_RULES = {
    "email": (re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"), "an email address like name@example.org"),
    "sms": (re.compile(r"\+?[0-9]{7,15}"), "a phone number of 7 to 15 digits, like +441234567890"),
    "print": (re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}"), "a printer name like front-desk"),
}


class ProviderError(Exception):
    """The provider could not deliver the message right now. The queue will retry."""


class ProviderTimeout(ProviderError):  # noqa: N818 - reads better at call sites
    """The provider did not answer in time."""


@dataclass(frozen=True)
class NotificationConfig:
    """Sections [notifications] of the config file."""

    email_driver: str = "mock"
    sms_driver: str = "mock"
    print_driver: str = "mock"
    max_attempts: int = 3  # delivery attempts before a message is marked failed
    backoff_s: float = 60.0  # wait between attempts grows with each failure: backoff x attempts so far
    reminders: bool = True  # tell patrons their loan is due soon / today (needs the member's e-mail or phone)
    remind_days_before: int = 1  # the "due soon" reminder goes out this many days before the due date
    auto_dispatch_s: float = 0.0  # > 0: the running app scans for reminders and delivers messages this often (0 = off)


@dataclass(frozen=True)
class Outgoing:
    channel: str
    recipient: str
    subject: str
    body: str
    ref: int  # the queue row id, so the provider's own log can be matched to ours


@runtime_checkable
class NotificationProvider(Protocol):
    channel: str

    def send(self, message: Outgoing) -> str:
        """Deliver the message. Returns the provider's own reference. Raises ProviderError to ask for a retry."""


def valid_address(channel: str, address: str) -> bool:
    rule = ADDRESS_RULES.get(channel)
    return bool(rule and rule[0].fullmatch(address or ""))


def address_hint(channel: str) -> str:
    return ADDRESS_RULES[channel][1]


class MockProvider:
    """Records what it was asked to send (the 'outbox') instead of sending it. Supports failure injection."""

    channel = "email"
    is_mock = True

    def __init__(self, **_options: object) -> None:
        self.outbox: list[dict] = []
        self._injected: list[str] = []
        self._lock = RLock()

    def fail_next(self, mode: str = "error", times: int = 1) -> None:
        if mode not in ("error", "timeout"):
            raise ValueError(f"unknown failure mode '{mode}'")
        self._injected.extend([mode] * times)

    def pending_failures(self) -> int:
        return len(self._injected)

    def clear_failures(self) -> None:
        self._injected.clear()

    def send(self, message: Outgoing) -> str:
        with self._lock:
            mode = self._injected.pop(0) if self._injected else ""
            if mode == "timeout":
                raise ProviderTimeout(f"mock {self.channel} provider timed out")
            if mode == "error":
                raise ProviderError(f"mock {self.channel} provider is unavailable")
            if not valid_address(self.channel, message.recipient):
                raise ProviderError(f"'{message.recipient}' is not {address_hint(self.channel)}")
            reference = f"mock-{self.channel}-{len(self.outbox) + 1}"
            self.outbox.append(
                {
                    "ref": reference,
                    "queue_id": message.ref,
                    "channel": self.channel,
                    "to": message.recipient,
                    "subject": message.subject,
                    "body": message.body,
                }
            )
            return reference


class MockEmailProvider(MockProvider):
    channel = "email"


class MockSmsProvider(MockProvider):
    channel = "sms"


class MockPrintProvider(MockProvider):
    """A 'printer': the outbox holds the slips it would have printed."""

    channel = "print"


class ProviderConfigError(ValueError):
    """The [notifications] configuration is invalid (fails fast at startup)."""


_DRIVERS: dict[tuple[str, str], Callable[..., NotificationProvider]] = {
    ("email", "mock"): MockEmailProvider,
    ("sms", "mock"): MockSmsProvider,
    ("print", "mock"): MockPrintProvider,
}


def register_provider(channel: str, name: str, factory: Callable[..., NotificationProvider]) -> None:
    """Add a real provider: `register_provider("email", "smtp", SmtpEmailProvider)`, then `email_driver = "smtp"`."""
    _DRIVERS[(channel, name)] = factory


def build_providers(config: NotificationConfig) -> dict[str, NotificationProvider]:
    chosen = {"email": config.email_driver, "sms": config.sms_driver, "print": config.print_driver}
    out: dict[str, NotificationProvider] = {}
    for channel, name in chosen.items():
        factory = _DRIVERS.get((channel, name))
        if factory is None:
            known = sorted(n for (c, n) in _DRIVERS if c == channel)
            raise ProviderConfigError(f"unknown {channel} provider '{name}' (known: {known})")
        provider = factory()
        if not isinstance(provider, NotificationProvider) or provider.channel != channel:
            raise ProviderConfigError(f"{channel} provider '{name}' does not implement NotificationProvider")
        out[channel] = provider
    return out
