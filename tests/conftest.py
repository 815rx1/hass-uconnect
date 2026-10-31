"""Fixtures for the Uconnect tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uconnect.const import (
    CONF_ADD_COMMAND_ENTITIES,
    CONF_BRAND_REGION,
    CONF_DISABLE_TLS_VERIFICATION,
    DOMAIN,
)
from custom_components.uconnect.pyuconnect.api import _LOGIN_BACKOFFS, _PIN_REJECTIONS
from custom_components.uconnect.pyuconnect.brands import FIAT_EU
from custom_components.uconnect.pyuconnect.client import (
    Client,
    Vehicle,
    _update_vehicle,
)

FIXTURES = Path(__file__).parent / "fixtures"
VIN = "ZFAEFAC39MX000001"


if sys.platform == "win32":
    # The Home Assistant test plugin blocks sockets to keep tests offline,
    # but the Windows event loop needs a socketpair to start at all. The block
    # stays in place on Linux, where the CI runs; Windows is only for local runs.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the integration from custom_components."""
    yield


@pytest.fixture(autouse=True)
def fresh_login_backoff():
    """The login backoff and a rejected PIN outlive API objects, but not a test."""
    _LOGIN_BACKOFFS.clear()
    _PIN_REJECTIONS.clear()
    yield
    _LOGIN_BACKOFFS.clear()
    _PIN_REJECTIONS.clear()


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_vehicle() -> Vehicle:
    """Return a 500e built from the sample status by the real parser."""
    vehicle = Vehicle(
        vin=VIN,
        nickname=None,
        make="FIAT",
        model="500e",
        year=2022,
        region="EMEA",
    )
    vehicle.fuel_type = "E"
    vehicle.supported_commands = [
        "RDL",
        "RDU",
        "HBLF",
        "ROPRECOND",
        "ROPRECOND_OFF",
        "DEEPREFRESH",
        "VF",
    ]
    vehicle.enabled_services = [*vehicle.supported_commands, "CPPLUS"]
    _update_vehicle(vehicle, load_fixture("status_500e.json"))
    return vehicle


@pytest.fixture
def vehicle() -> Vehicle:
    return make_vehicle()


@pytest.fixture
def cloud_status() -> dict:
    """The status the mocked cloud answers with, tests may change it."""
    return load_fixture("status_500e.json")


@pytest.fixture
def mock_client(vehicle, cloud_status):
    """Patch the API client used by the coordinator."""
    client = MagicMock()
    client.api.sess = requests.Session()
    client.api.pin = "1234"
    client.api.pin_rejected = False
    client.api.pin_locked = False
    client.api.pin_rejection_fingerprint.return_value = "fp"
    client.confirmed_states.return_value = {}
    client.placeholders = {}
    client.last_charges.return_value = {}
    client.vehicles = {VIN: vehicle}
    client.get_vehicles.return_value = client.vehicles
    client.get_vehicle_health_report.return_value = {}
    client.get_maintenance_history.return_value = {}
    client.get_stolen_vehicle_status.return_value = {}
    client.get_vehicle_image.return_value = {}
    client.command_verify.return_value = True
    client.set_charge_schedule_emea.return_value = True

    # Schedule changes are planned by the real library code, against what the
    # mocked cloud answers right then
    planner = Client("user@example.com", "pw", "1234", FIAT_EU)
    planner.api = MagicMock()
    planner.api.get_vehicle.side_effect = lambda vin: copy.deepcopy(cloud_status)
    client.plan_charge_schedule_emea.side_effect = planner.plan_charge_schedule_emea
    client.send_charge_schedules_emea.return_value = True
    with patch(
        "custom_components.uconnect.coordinator.Client", return_value=client
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="FIAT_EU user@example.com",
        unique_id="abc123",
        data={
            "username": "user@example.com",
            "password": "secret",
            CONF_BRAND_REGION: 1,
            "pin": "1234",
            CONF_DISABLE_TLS_VERIFICATION: False,
        },
        options={
            "scan_interval": 5,
            "charging_refresh_interval": 0,
            CONF_ADD_COMMAND_ENTITIES: True,
        },
    )


@pytest.fixture
async def setup_integration(hass, config_entry, mock_client):
    """Set up the integration with the mocked client."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
