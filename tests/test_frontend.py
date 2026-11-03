"""Tests for the vehicle image download and the dashboard card loading."""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er

from custom_components.uconnect import frontend_cards
from custom_components.uconnect.image import BROWSER_HEADERS, UconnectVehicleImage

PNG = bytes.fromhex("89504e470d0a1a0a") + b"rest"
CARD_URL = "/uconnect/uconnect-vehicle-card.js?v=" + json.loads(
    (
        Path(__file__).parent.parent / "custom_components" / "uconnect" / "manifest.json"
    ).read_text(encoding="utf-8")
)["version"]


class FakeResponse:
    def __init__(
        self, status: int, body: bytes = b"", content_type: str = "", hang: bool = False
    ):
        self.status = status
        self._body = body
        self.content_type = content_type
        self._hang = hang

    async def read(self) -> bytes:
        if self._hang:
            await asyncio.Event().wait()
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


async def test_image_download_ends_before_home_assistant_gives_up(hass):
    # Home Assistant cancels it after 10 s, before the stale image is served
    # and the retry paused
    with patch("custom_components.uconnect.image.IMAGE_FETCH_TIMEOUT", 0.05):
        _, session, data = await asyncio.wait_for(
            fetch(hass, [FakeResponse(200, PNG, "image/png", hang=True)]), 2
        )
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
    add.assert_called_once_with(hass, CARD_URL)
    assert domain_data[frontend_cards._EXTRA_JS_KEY] == CARD_URL
    assert frontend_cards._RESOURCE_ID_KEY not in domain_data

    # Removed under the same URL
    config_entry = SimpleNamespace(entry_id="gone")
    hass.data[frontend_cards.DOMAIN] = domain_data
    with patch.object(frontend_cards, "remove_extra_js_url") as remove:
        await frontend_cards.async_remove_frontend_cards_if_last_entry(hass, config_entry)
    remove.assert_called_once_with(hass, CARD_URL)


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
    assert created == [{"res_type": "module", "url": CARD_URL}]


async def test_card_resource_of_an_older_version_is_updated(hass):
    # Without a version, as the original and older versions registered it: a
    # browser keeps the card under that URL for up to a month
    items = [
        {"id": "other", "type": "module", "url": "/local/other-card.js"},
        {"id": "res1", "type": "module", "url": "/uconnect/uconnect-vehicle-card.js"},
    ]
    updated = []

    async def update(item_id, changes):
        updated.append((item_id, changes))
        item = next(x for x in items if x["id"] == item_id)
        item["url"] = changes["url"]
        return item

    async def create(item):
        raise AssertionError("a second resource was created")

    hass.data["lovelace"] = SimpleNamespace(
        resources=SimpleNamespace(
            loaded=True,
            async_items=lambda: items,
            async_create_item=create,
            async_update_item=update,
        )
    )
    domain_data: dict = {}
    await frontend_cards._async_load_card(hass, domain_data)
    await frontend_cards._async_load_card(hass, domain_data)

    # The same resource, under the new URL, changed only once
    assert domain_data[frontend_cards._RESOURCE_ID_KEY] == "res1"
    assert updated == [("res1", {"res_type": "module", "url": CARD_URL})]
    assert await frontend_cards._async_find_lovelace_resource(hass) == "res1"


async def test_card_gets_the_doors_lock(hass, setup_integration):
    from custom_components.uconnect.const import DOMAIN

    from .conftest import VIN

    lock = er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{DOMAIN}_{VIN}_lock_doors")
    vehicle = frontend_cards._build_vehicle_list(hass)[0]
    assert lock and vehicle["entities"]["doors_lock"] == lock


# ---- The card itself, run in a headless Chromium ----

CARD_JS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "uconnect"
    / "frontend"
    / "uconnect-vehicle-card.js"
)


def _browser() -> str | None:
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        if found := shutil.which(name):
            return found
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
        for path in (
            "Microsoft/Edge/Application/msedge.exe",
            "Google/Chrome/Application/chrome.exe",
        ):
            if base and (candidate := Path(base) / path).is_file():
                return str(candidate)
    return None


CARD_HARNESS = """<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<div id="host"></div>
<script src="card.js"></script>
<script>
const E = %(entities)s;
const st = (key, state, attributes = {}) => ({ entity_id: E[key], state, attributes });
const hassOf = (states) => ({
  states: Object.fromEntries(states.map((s) => [s.entity_id, s])),
  entities: {},
  callWS: async () => ({
    vehicles: [{ vin: "VIN", device_id: "dev1", name: "FIAT 500e", entities: E }],
  }),
});
const render = async (states) => {
  const el = document.createElement("uconnect-vehicle-card");
  el.setConfig({ device_id: "dev1", show_map: false });
  document.getElementById("host").replaceChildren(el);
  el.hass = hassOf(states);
  await new Promise((r) => setTimeout(r, 50));
  return el;
};
const indicators = (el) => [...el.shadowRoot.querySelectorAll(".indicator")]
  .map((b) => `${b.title} [${b.className.replace("indicator", "").trim()}]`);
const buttons = (el) => [...el.shadowRoot.querySelectorAll(".btn-item")]
  .map((b) => `${b.querySelector(".btn-title").textContent}=`
    + `${b.querySelector(".btn-value").textContent}`
    + (b.classList.contains("alert") ? " [alert]" : ""));
(async () => {
  let result;
  try {
    result = await (async () => { %(script)s })();
  } catch (err) {
    result = { error: String(err && err.stack || err) };
  }
  const out = document.createElement("pre");
  out.id = "out";
  out.textContent = JSON.stringify(result);
  document.body.appendChild(out);
})();
</script></body></html>
"""

CARD_ENTITIES = {
    key: f"{platform}.fiat_500e_{key}"
    for platform, keys in (
        (
            "binary_sensor",
            (
                "door_driver",
                "door_passenger",
                "window_driver",
                "window_passenger",
                "tire_fl_warn",
                "tire_fr_warn",
                "tire_rl_warn",
                "tire_rr_warn",
            ),
        ),
        ("sensor", ("soc", "range", "tire_fl", "tire_fr", "tire_rl", "tire_rr")),
        ("device_tracker", ("device_tracker",)),
    )
    for key in keys
}


def run_card(tmp_path, script: str, entities: dict | None = None):
    """Run script against the card in a headless browser, return its result."""
    browser = _browser()
    if browser is None:
        pytest.skip("no Chromium based browser to run the card in")
    shutil.copy(CARD_JS, tmp_path / "card.js")
    page = tmp_path / "card.html"
    page.write_text(
        CARD_HARNESS
        % {"entities": json.dumps(entities or CARD_ENTITIES), "script": script},
        encoding="utf-8",
    )
    dom = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--virtual-time-budget=5000",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    ).stdout
    start = dom.index('<pre id="out">') + len('<pre id="out">')
    result = json.loads(html.unescape(dom[start : dom.index("</pre>", start)]))
    assert not (isinstance(result, dict) and "error" in result), result
    return result


def test_card_door_without_state_is_not_shown_locked(tmp_path):
    result = run_card(
        tmp_path,
        """
        // The passenger door and window sensors are disabled: no state at all
        const disabled = indicators(await render([
          st("door_driver", "off"), st("window_driver", "off"),
        ])).slice(0, 2);
        const open = indicators(await render([
          st("door_driver", "off"), st("window_passenger", "on"),
        ])).slice(0, 2);
        const all = indicators(await render([
          st("door_driver", "off"), st("door_passenger", "off"),
          st("window_driver", "off"), st("window_passenger", "off"),
        ])).slice(0, 2);
        return { disabled, open, all };
        """,
    )
    assert result["disabled"] == ["Doors: Unknown []", "Windows: Unknown []"]
    assert result["open"] == ["Doors: Unknown []", "Windows: 1 open [alert]"]
    assert result["all"] == ["Doors: Locked [ok]", "Windows: Closed [ok]"]


def test_card_doors_follow_the_doors_lock(tmp_path):
    entities = {**CARD_ENTITIES, "doors_lock": "lock.fiat_500e_doors_lock"}
    result = run_card(
        tmp_path,
        """
        // The lock takes in a door that has no sensor
        const unlocked = indicators(await render([
          st("door_driver", "off"), st("door_passenger", "off"),
          st("doors_lock", "unlocked"),
        ]))[0];
        const locked = indicators(await render([
          st("door_driver", "off"), st("doors_lock", "locked"),
        ]))[0];
        // Unknown, the doors are looked at instead
        const unknown = indicators(await render([
          st("door_driver", "off"), st("door_passenger", "on"),
          st("doors_lock", "unknown"),
        ]))[0];
        return { unlocked, locked, unknown };
        """,
        entities,
    )
    assert result == {
        "unlocked": "Doors: Unlocked [alert]",
        "locked": "Doors: Locked [ok]",
        "unknown": "Doors: Unlocked [alert]",
    }


def test_card_shows_the_tire_warnings_next_to_pressures(tmp_path):
    result = run_card(
        tmp_path,
        """
        const tires = (pressures, warnings) => [
          ...["tire_fl", "tire_fr", "tire_rl", "tire_rr"].map(
            (key, i) => st(key, pressures[i], { unit_of_measurement: "bar" })),
          ...["tire_fl_warn", "tire_fr_warn", "tire_rl_warn", "tire_rr_warn"].map(
            (key, i) => st(key, warnings[i])),
        ];
        const tire = async (states) => buttons(await render(states))
          .filter((b) => b.startsWith("Tire"));
        return {
          one: await tire(tires(["2.3", "2.3", "2.0", "2.3"], ["off", "off", "on", "off"])),
          all: await tire(tires(["1.6", "1.6", "1.6", "1.6"], ["on", "on", "on", "on"])),
          none: await tire(tires(["2.3", "2.3", "2.0", "2.3"], ["off", "off", "off", "off"])),
          low: await tire(tires(["2.3", "2.3", "1.5", "2.3"], ["off", "off", "off", "off"])),
        };
        """,
    )
    assert result == {
        "one": ["Tire RL=2.00 bar [alert]"],
        "all": ["Tire FL=1.60 bar [alert]"],
        "none": ["Tires=2.22 bar"],
        "low": ["Tire RL=1.50 bar [alert]"],
    }


def test_card_buttons_are_not_rebuilt_for_a_quote(tmp_path):
    result = run_card(
        tmp_path,
        """
        const rebuilt = {};
        for (const zone of ["Oma's Haus", 'Bei "Paul"', "Arbeit"]) {
          const states = [st("device_tracker", zone)];
          const el = await render(states);
          const first = el.shadowRoot.getElementById("buttons").firstElementChild;
          el.hass = hassOf(states);
          rebuilt[zone] = el.shadowRoot.getElementById("buttons").firstElementChild !== first;
        }
        // A changed text is still written
        const el = await render([st("device_tracker", "Oma's Haus")]);
        el.hass = hassOf([st("device_tracker", "Arbeit")]);
        rebuilt.changed = buttons(el)[0];
        return rebuilt;
        """,
    )
    assert result == {
        "Oma's Haus": False,
        'Bei "Paul"': False,
        "Arbeit": False,
        "changed": "Location=Arbeit",
    }


def test_card_ev_range_stays_an_ev_range(tmp_path):
    result = run_card(
        tmp_path,
        """
        const range = async (states) => buttons(await render(states))
          .filter((b) => b.includes("Range"));
        const km = { unit_of_measurement: "km" };
        return {
          unknown: await range([st("soc", "unknown"), st("range", "150", km)]),
          known: await range([st("soc", "80"), st("range", "150", km)]),
        };
        """,
    )
    assert result == {"unknown": ["Range=150 km"], "known": ["Range=150 km"]}

    # A combustion car still gets its fuel range
    fuel = {k: v for k, v in CARD_ENTITIES.items() if k != "soc"}
    fuel["fuel"] = "sensor.fiat_500e_fuel"
    result = run_card(
        tmp_path,
        """
        return buttons(await render([st("range", "500", { unit_of_measurement: "km" })]))
          .filter((b) => b.includes("Range"));
        """,
        fuel,
    )
    assert result == ["Fuel Range=500 km"]
