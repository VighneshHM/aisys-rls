"""NFR06: configuration lives outside the code, is checked at start, and the sample file is always loadable."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfsense.config import load_settings
from shelfsense.rfid.registry import DeviceRegistry

pytestmark = pytest.mark.req("NFR06")
SAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.sample.toml"


def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return str(p)


def test_the_sample_config_loads_and_builds_every_device():
    s = load_settings(str(SAMPLE))
    kinds = {d.kind for d in DeviceRegistry(s.devices).all()}
    assert kinds == {"staff_reader", "handheld", "gate", "camera", "card_printer"}
    assert s.circulation.ilms_driver == "mock" and s.opac_enabled and s.netcat_driver == "mock"
    assert s.notifications.reminders and s.notifications.auto_dispatch_s == 60
    assert s.tls_certfile == "" and s.tls_keyfile == ""


def test_the_ilms_address_is_configurable(tmp_path):
    s = load_settings(write(tmp_path, '[ilms]\nendpoint = " 192.168.1.50 "\nport = 6001\nprotocol = "sip2"\n'))
    c = s.circulation
    assert (c.ilms_endpoint, c.ilms_port) == ("192.168.1.50", 6001)
    assert load_settings(write(tmp_path, '[ilms]\nport = "6001"\n')).circulation.ilms_port == 6001, (
        "a quoted number works too"
    )
    none = load_settings(write(tmp_path, "")).circulation
    assert (none.ilms_endpoint, none.ilms_port) == ("", 0)


@pytest.mark.parametrize("port", ["abc", "70000", "-5", "1.5e3"])
def test_a_bad_ilms_port_stops_the_start_with_a_plain_message(tmp_path, port):
    with pytest.raises(RuntimeError, match="port must be a number from 1 to 65535"):
        load_settings(write(tmp_path, f'[ilms]\nport = "{port}"\n'))


def test_environment_variables_override_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, '[app]\nport = 9000\n[database]\nurl = "sqlite:///from-file.db"\n')
    monkeypatch.setenv("SHELFSENSE_PORT", "9100")
    monkeypatch.setenv("SHELFSENSE_DATABASE_URL", "sqlite:///from-env.db")
    monkeypatch.setenv("SHELFSENSE_BACKUP_DIR", "elsewhere")
    s = load_settings(path)
    assert (s.port, s.database_url, s.backup_dir) == (9100, "sqlite:///from-env.db", "elsewhere")


def test_production_refuses_a_short_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSENSE_ENV", "production")
    monkeypatch.setenv("SHELFSENSE_SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="SHELFSENSE_SECRET_KEY"):
        load_settings(write(tmp_path, ""))


@pytest.mark.req("FR03")
def test_an_ncip_protocol_setting_is_refused_rather_than_mislabelling_sip2(tmp_path):
    with pytest.raises(ValueError, match="only SIP2 is supported"):
        load_settings(write(tmp_path, '[ilms]\nprotocol = "ncip"\n'))
    assert load_settings(write(tmp_path, "[ilms]\n")).circulation.ilms_driver == "mock"  # no line is fine
