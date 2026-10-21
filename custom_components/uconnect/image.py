"""Image platform for Uconnect integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path

import aiohttp

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .pyuconnect.client import Vehicle

from .const import DOMAIN
from .coordinator import UconnectDataUpdateCoordinator
from .entity import UconnectEntity, was_added

_LOGGER = logging.getLogger(__name__)

CACHE_DIR = ".storage/uconnect"
IMAGE_RETRY_AFTER = timedelta(hours=1)

# The image CDNs are fronted by Akamai, which rejects some User-Agents with a
# 403: the Ram one blocks non-browser agents (#116), the Fiat one blocks the
# browser agent (#127). Each set of headers is tried until one is accepted,
# the empty one sending the default Home Assistant User-Agent.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/*,*/*",
}
FETCH_HEADERS: tuple[dict[str, str], ...] = (BROWSER_HEADERS, {})


def _sniff_content_type(data: bytes) -> str:
    """Return the image type from the first bytes of a file."""
    if data.startswith(bytes.fromhex("89504e47")):  # PNG
        return "image/png"
    if data.startswith(bytes.fromhex("ffd8")):  # JPEG
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return "image/png"


PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Uconnect image entities."""
    coordinator: UconnectDataUpdateCoordinator = config_entry.runtime_data

    entities = []
    for vehicle in coordinator.client.get_vehicles().values():
        image_url = getattr(vehicle, "image_url", None)

        if not image_url:
            # Try the dedicated image API endpoint
            try:
                result = await coordinator.async_run(
                    coordinator.client.get_vehicle_image, vehicle.vin
                )
                _LOGGER.debug(
                    "get_vehicle_image response for %s: %s", vehicle.vin, result
                )
                if isinstance(result, dict):
                    items = result.get("items")
                    if isinstance(items, list) and items:
                        image_url = items[0].get("preciseImageURL")
            except Exception as err:
                response = getattr(err, "response", None)
                status = getattr(response, "status_code", None)
                if status in (400, 404):
                    _LOGGER.debug(
                        "Vehicle image unavailable for %s: HTTP %s",
                        vehicle.vin,
                        status,
                    )
                else:
                    _LOGGER.error(
                        "Error calling get_vehicle_image for %s: %s: %s",
                        vehicle.vin,
                        type(err).__name__,
                        err,
                    )

        if image_url or was_added(hass, "image", vehicle.vin, "image"):
            entities.append(
                UconnectVehicleImage(coordinator, vehicle, hass, image_url or "")
            )

    async_add_entities(entities)


class UconnectVehicleImage(UconnectEntity, ImageEntity):
    """Representation of a Uconnect vehicle image."""

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
        hass: HomeAssistant,
        image_url: str,
    ) -> None:
        """Initialize the image entity."""
        UconnectEntity.__init__(self, coordinator, vehicle)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_image"
        self._attr_name = f"{vehicle.make} {vehicle.nickname or vehicle.model} Image"
        self._attr_icon = "mdi:car"
        self._image_url = image_url
        cache_dir = Path(hass.config.path(CACHE_DIR))
        self._cache_path = cache_dir / f"{vehicle.vin}.img"
        self._url_path = cache_dir / f"{vehicle.vin}.url"
        self._cached_url: str | None = None
        self._failed_at = None

    async def async_added_to_hass(self) -> None:
        """Restore cached image state on startup."""
        await super().async_added_to_hass()
        cached_url, mtime = await self.hass.async_add_executor_job(
            self._read_cache_info
        )
        if cached_url and cached_url == self._image_url:
            self._cached_url = cached_url
        # The cached file's age, so a restart is not recorded as a new image
        self._attr_image_last_updated = (
            dt_util.utc_from_timestamp(mtime) if mtime else dt_util.utcnow()
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Pick up a changed image URL from the vehicle list."""
        url = getattr(self.vehicle, "image_url", None)
        if url and url != self._image_url:
            self._image_url = url
            self._failed_at = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return vehicle image bytes, downloading and caching as needed."""
        url = self._image_url
        if not url:
            return None

        # Serve from disk cache if URL matches
        if url == self._cached_url:
            data = await self.hass.async_add_executor_job(self._read_cache)
            if data is not None:
                return data

        # A refused download is not retried on every dashboard view
        if self._failed_at and dt_util.utcnow() - self._failed_at < IMAGE_RETRY_AFTER:
            return await self.hass.async_add_executor_job(self._read_cache)

        # Download and cache
        data = await self._fetch_image(url)
        if data is not None:
            await self.hass.async_add_executor_job(self._write_cache, data, url)
            self._cached_url = url
            self._failed_at = None
            return data

        # Fall back to stale cache on download failure
        self._failed_at = dt_util.utcnow()
        return await self.hass.async_add_executor_job(self._read_cache)

    async def _fetch_image(self, url: str) -> bytes | None:
        """Download image from URL."""
        try:
            session = async_get_clientsession(self.hass)
            for headers in FETCH_HEADERS:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 403:
                        _LOGGER.debug(
                            "Vehicle image rejected with HTTP 403 for User-Agent %s",
                            headers.get("User-Agent", "default"),
                        )
                        continue
                    if resp.status != 200:
                        _LOGGER.warning(
                            "Failed to fetch vehicle image: HTTP %s", resp.status
                        )
                        return None
                    data = await resp.read()
                    # Missing headers are reported as application/octet-stream
                    if resp.content_type and resp.content_type.startswith("image/"):
                        self._attr_content_type = resp.content_type
                    else:
                        self._attr_content_type = _sniff_content_type(data)
                    return data
        except Exception:
            _LOGGER.exception("Error fetching vehicle image")
            return None

        _LOGGER.warning("Failed to fetch vehicle image: HTTP 403")
        return None

    def _read_cache(self) -> bytes | None:
        """Read cached image from disk."""
        if self._cache_path.is_file():
            data = self._cache_path.read_bytes()
            # The content type is only known from a download, so after a
            # restart it is taken from the file itself
            self._attr_content_type = _sniff_content_type(data)
            return data
        return None

    def _read_cache_info(self) -> tuple[str | None, float | None]:
        """Read the URL and modification time of the cached image."""
        mtime = self._cache_path.stat().st_mtime if self._cache_path.is_file() else None
        return self._read_cached_url(), mtime

    def _read_cached_url(self) -> str | None:
        """Read the URL of the cached image."""
        if self._url_path.is_file():
            return self._url_path.read_text().strip()
        return None

    def _write_cache(self, data: bytes, url: str) -> None:
        """Write image and its source URL to disk."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(data)
        self._url_path.write_text(url)
