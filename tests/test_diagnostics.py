"""Tests for the diagnostics download."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from homeassistant.helpers.json import JSONEncoder

from custom_components.uconnect.diagnostics import async_get_config_entry_diagnostics

from .conftest import VIN, load_fixture


async def test_diagnostics_are_redacted(hass, setup_integration, mock_client):
    mock_client._last_status = {VIN: load_fixture("status_500e.json")}
    mock_client.vehicles[VIN].nickname = "Kleiner"
    mock_client.vehicles[VIN].location = None
    uploaded = datetime(2026, 10, 28, 17, 50, tzinfo=timezone.utc)
    mock_client.placeholders[VIN] = {"uploaded": uploaded, "range": 171}
    mock_client.last_charges.return_value = {VIN: {"state_of_charge": 72}}

    result = await async_get_config_entry_diagnostics(hass, setup_integration)
    dumped = json.dumps(result, cls=JSONEncoder)

    for secret in (VIN, "user@example.com", "\"secret\"", "\"1234\"", "Kleiner"):
        assert secret not in dumped, secret

    vehicle = result["vehicles"][0]
    assert vehicle["parsed"]["state_of_charge"] == 72
    assert vehicle["raw_status"]["evInfo"]["battery"]["stateOfCharge"] == 72
    assert vehicle["last_placeholder"] == {"uploaded": uploaded, "range": 171}
    assert vehicle["last_real_charge"] == {"state_of_charge": 72}
    assert result["coordinator"]["pin_set"] is True
    assert result["entry"]["options"]["scan_interval"] == 5
