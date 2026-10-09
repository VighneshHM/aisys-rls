"""Builds the ILMS connector from configuration.

To support a real ILMS: write a class implementing `CirculationConnector`, call `register_driver("sip2-tcp", Adapter)`,
and set `driver = "sip2-tcp"` under [ilms] in the config file. Nothing else in the application changes.
"""

from __future__ import annotations

from collections.abc import Callable

from aisys.circulation.connector import CirculationConfig, CirculationConnector
from aisys.circulation.mock_ilms import MockIlms
from aisys.circulation.resilience import ResilientConnector
from aisys.circulation.sip2.adapter import Sip2Connector

_DRIVERS: dict[str, Callable[..., CirculationConnector]] = {"mock": MockIlms}


class ConnectorConfigError(ValueError):
    """The [ilms] configuration is invalid (fails fast at startup)."""


def _sip2(config: CirculationConfig, **_options: object) -> CirculationConnector:
    if not config.ilms_endpoint or not config.ilms_port:
        raise ConnectorConfigError("the sip2 driver needs [ilms] endpoint and port")
    return Sip2Connector(config)


_DRIVERS["sip2"] = _sip2


def register_driver(name: str, factory: Callable[..., CirculationConnector]) -> None:
    _DRIVERS[name] = factory


def build_connector(config: CirculationConfig) -> ResilientConnector:
    factory = _DRIVERS.get(config.ilms_driver)
    if factory is None:
        raise ConnectorConfigError(f"unknown ILMS driver '{config.ilms_driver}' (known: {sorted(_DRIVERS)})")
    inner = factory(config=config)
    if not isinstance(inner, CirculationConnector):
        raise ConnectorConfigError(f"ILMS driver '{config.ilms_driver}' does not implement CirculationConnector")
    return ResilientConnector(inner, retries=config.ilms_retries, backoff_s=config.ilms_backoff_s)
