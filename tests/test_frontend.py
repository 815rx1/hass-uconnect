"""Tests for the vehicle image download and the dashboard card loading."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import CoreState

from custom_components.uconnect import frontend_cards
from custom_components.uconnect.image import BROWSER_HEADERS, UconnectVehicleImage

PNG = bytes.fromhex("89504e470d0a1a0a") + b"rest"


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", content_type: str = ""):
        self.status = status
        self._body = body
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = []

    def get(self, url, headers=None, timeout=None):
        self.headers.append(headers)
        return self.responses.pop(0)


def image_entity(hass) -> UconnectVehicleImage:
    entity = UconnectVehicleImage.__new__(UconnectVehicleImage)
    entity.hass = hass
    entity._attr_content_type = "image/jpeg"
    return entity


async def fetch(hass, responses):
    session = FakeSession(responses)
    entity = image_entity(hass)
    with patch(
        "custom_components.uconnect.image.async_get_clientsession", return_value=session
    ):
        data = await entity._fetch_image("https://example.invalid/car.png")
    return entity, session, data


async def test_image_retries_with_default_agent_after_403(hass):
    entity, session, data = await fetch(
        hass, [FakeResponse(403), FakeResponse(200, PNG, "application/octet-stream")]
    )

    assert data == PNG
    # First the browser agent, then Home Assistant's own
    assert session.headers == [BROWSER_HEADERS, {}]
    # A non-image content type is replaced by the sniffed one
    assert entity._attr_content_type == "image/png"


async def test_image_with_browser_agent_needs_one_request(hass):
    entity, session, data = await fetch(hass, [FakeResponse(200, PNG, "image/webp")])

    assert data == PNG and len(session.headers) == 1
    assert entity._attr_content_type == "image/webp"


async def test_image_rejected_by_both_agents(hass):
    _, session, data = await fetch(hass, [FakeResponse(403), FakeResponse(403)])
    assert data is None and len(session.headers) == 2


async def test_image_other_error_is_not_retried(hass):
    _, session, data = await fetch(hass, [FakeResponse(500), FakeResponse(200, PNG)])
    assert data is None and len(session.headers) == 1


async def test_card_falls_back_to_extra_js_in_yaml_mode(hass):
    # YAML mode: the resource collection has no async_create_item
    hass.data["lovelace"] = SimpleNamespace(
        resources=SimpleNamespace(loaded=True, async_items=lambda: [])
    )
    hass.set_state(CoreState.running)
    domain_data: dict = {}

    with patch.object(frontend_cards, "add_extra_js_url") as add:
        await frontend_cards._async_load_card(hass, domain_data)
        await frontend_cards._async_load_card(hass, domain_data)

    # Added once, not again on a reload
    add.assert_called_once_with(hass, "/uconnect/uconnect-vehicle-card.js")
    assert domain_data[frontend_cards._EXTRA_JS_KEY] is True
    assert frontend_cards._RESOURCE_ID_KEY not in domain_data


async def test_card_registers_resource_in_storage_mode(hass):
    created = []

    async def create(item):
        created.append(item)
        return {"id": "res1", **item}

    hass.data["lovelace"] = SimpleNamespace(
        resources=SimpleNamespace(
            loaded=True, async_items=lambda: [], async_create_item=create
        )
    )
    domain_data: dict = {}

    with patch.object(frontend_cards, "add_extra_js_url") as add:
        await frontend_cards._async_load_card(hass, domain_data)

    add.assert_not_called()
    assert domain_data[frontend_cards._RESOURCE_ID_KEY] == "res1"
    assert created == [{"res_type": "module", "url": "/uconnect/uconnect-vehicle-card.js"}]
