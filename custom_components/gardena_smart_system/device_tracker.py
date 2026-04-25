"""Support for Gardena Smart System mower device tracker."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp
from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.components.lawn_mower import LawnMowerActivity

from .const import DOMAIN, MOWER_ACTIVITY_MAP
from .coordinator import GardenaSmartSystemCoordinator
from .entities import GardenaEntity

_LOGGER = logging.getLogger(__name__)

# Activities that mean the mower is actively out on the lawn.
# Derived from MOWER_ACTIVITY_MAP so this set stays in sync with const.py
# and never misses activities like OK_CUTTING_TIMER_OVERRIDDEN.
_MOWING_ACTIVITIES = frozenset(
    activity
    for activity, ha_activity in MOWER_ACTIVITY_MAP.items()
    if ha_activity == LawnMowerActivity.MOWING
)

# How often (in seconds) to poll for a fresh GPS position while mowing.
_POLL_INTERVAL = 30

# How often (in seconds) to refresh the last-known position while docked/idle.
# A less-frequent poll keeps coordinates up-to-date so the entity always has
# a valid lat/lon for the HA map card, even when the mower is not mowing.
_IDLE_POLL_INTERVAL = 600

# How often (in seconds) the position keepalive PUT must be re-sent.
# The undocumented API deactivates the stream after ~10 minutes of silence,
# so we refresh every 8 minutes to stay well within that window.
_KEEPALIVE_INTERVAL = 480


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gardena Smart System mower device trackers."""
    coordinator: GardenaSmartSystemCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for location in coordinator.locations.values():
        for device in location.devices.values():
            if "MOWER" in device.services:
                for mower_service in device.services["MOWER"]:
                    _LOGGER.debug(
                        "Creating mower tracker for device %s (%s)",
                        device.name, device.id,
                    )
                    entities.append(GardenaMowerTracker(coordinator, device, mower_service))

    async_add_entities(entities)


class GardenaMowerTracker(GardenaEntity, TrackerEntity):
    """Device tracker that reports a Gardena mower's GPS position on a map."""

    def __init__(self, coordinator: GardenaSmartSystemCoordinator, device, mower_service) -> None:
        """Initialize the mower tracker."""
        super().__init__(coordinator, device, "MOWER")
        self._attr_name = f"{device.name} Mower Tracker"
        self._attr_unique_id = f"{device.id}_mower_tracker"
        self._mower_service = mower_service
        self._device_id = device.id

        # GPS state maintained by the polling loop.
        self._latitude: Optional[float] = None
        self._longitude: Optional[float] = None
        # ID of the ``lona`` ability discovered on the first position fetch.
        self._lona_ability_id: Optional[str] = None
        self._tracking_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # TrackerEntity interface
    # ------------------------------------------------------------------

    @property
    def source_type(self) -> SourceType:
        """Return the source type of the device tracker."""
        return SourceType.GPS

    @property
    def latitude(self) -> Optional[float]:
        """Return the current latitude of the mower."""
        return self._latitude

    @property
    def longitude(self) -> Optional[float]:
        """Return the current longitude of the mower."""
        return self._longitude

    # ------------------------------------------------------------------
    # Extra attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity-specific state attributes."""
        attrs = super().extra_state_attributes
        current_service = self._get_current_mower_service()
        if current_service:
            attrs["activity"] = current_service.activity
            attrs["state"] = current_service.state
        return attrs

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Start the position-tracking loop when the entity is added to HA."""
        await super().async_added_to_hass()
        self._tracking_task = self.hass.async_create_task(
            self._position_tracking_loop(),
            name=f"gardena_mower_tracker_{self._device_id}",
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the position-tracking loop when the entity is removed."""
        if self._tracking_task and not self._tracking_task.done():
            self._tracking_task.cancel()
            try:
                await self._tracking_task
            except asyncio.CancelledError:
                pass
        self._tracking_task = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_current_mower_service(self):
        """Return the current mower service from the coordinator's fresh data."""
        device = self.coordinator.get_device_by_id(self._device_id)
        if device and "MOWER" in device.services:
            for service in device.services["MOWER"]:
                if service.id == self._mower_service.id:
                    return service
        return None

    def _is_mowing(self) -> bool:
        """Return True when the mower is actively out on the lawn."""
        current_service = self._get_current_mower_service()
        if current_service and current_service.activity:
            return current_service.activity in _MOWING_ACTIVITIES
        return False

    def _update_position_from_data(self, device_data: dict) -> bool:
        """Parse position data from the undocumented API response.

        Updates ``_latitude``, ``_longitude``, and ``_lona_ability_id`` in place.
        Returns ``True`` when valid coordinates were found.
        """
        for ability in device_data.get("abilities", []):
            if ability.get("name") == "lona":
                self._lona_ability_id = ability.get("id")
                for prop in ability.get("properties", []):
                    if prop.get("name") == "position":
                        value = prop.get("value", {})
                        lat = value.get("gnssLatitude")
                        lon = value.get("gnssLongitude")
                        # Prefer real-time coordinates when available.
                        if value.get("isRealTimeReady"):
                            lat = value.get("realTimeLatitude", lat)
                            lon = value.get("realTimeLongitude", lon)
                        # Reject (0, 0) – the Gardena API returns these
                        # placeholder values when the mower has no GPS fix
                        # (e.g. while docked indoors).  Treating them as
                        # valid would pin the entity to the middle of the
                        # Atlantic Ocean and break the HA map display.
                        if lat is not None and lon is not None and (lat != 0 or lon != 0):
                            self._latitude = lat
                            self._longitude = lon
                            return True
                break
        return False

    async def _fetch_and_update_position(self) -> None:
        """Fetch the mower's GPS position from the API and push a state update."""
        device = self.coordinator.get_device_by_id(self._device_id)
        if not device:
            return

        try:
            device_data = await self.coordinator.client.fetch_mower_position(
                self._device_id, device.location_id
            )
            if device_data is not None and self._update_position_from_data(device_data):
                self.async_write_ha_state()
        except aiohttp.ClientError as exc:
            _LOGGER.debug("Network error fetching position for mower %s: %s", self._device_id, exc)
        except Exception:
            _LOGGER.warning("Unexpected error fetching position for mower %s", self._device_id, exc_info=True)

    async def _activate_position_stream(self) -> None:
        """Send a keepalive PUT so the GPS event stream stays active."""
        if self._lona_ability_id is None:
            return
        device = self.coordinator.get_device_by_id(self._device_id)
        if not device:
            return
        try:
            await self.coordinator.client.activate_mower_position(
                self._device_id, device.location_id, self._lona_ability_id
            )
        except aiohttp.ClientError as exc:
            _LOGGER.debug("Network error sending position keepalive for mower %s: %s", self._device_id, exc)
        except Exception:
            _LOGGER.warning("Unexpected error sending position keepalive for mower %s", self._device_id, exc_info=True)

    async def _position_tracking_loop(self) -> None:
        """Background task that polls for the mower's GPS position.

        While mowing, position is refreshed every ``_POLL_INTERVAL`` seconds so
        the map tracks the mower in near-real-time.  While idle/docked, position
        is refreshed every ``_IDLE_POLL_INTERVAL`` seconds so the entity always
        has valid coordinates for the HA map card regardless of mowing state.
        """
        _LOGGER.debug("Position tracking started for mower %s", self._device_id)

        # Fetch an initial position to discover the lona_ability_id and provide
        # a baseline coordinate before the first timed poll fires.
        await self._fetch_and_update_position()
        if self._lona_ability_id is not None:
            await self._activate_position_stream()

        seconds_since_keepalive = 0
        seconds_since_idle_poll = 0

        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL)
                seconds_since_keepalive += _POLL_INTERVAL
                seconds_since_idle_poll += _POLL_INTERVAL

                if self._is_mowing():
                    await self._fetch_and_update_position()
                    seconds_since_idle_poll = 0
                    _LOGGER.debug("Position updated for mower %s", self._device_id)
                elif seconds_since_idle_poll >= _IDLE_POLL_INTERVAL:
                    # Refresh last-known position while docked/idle so the
                    # entity always has valid coordinates for the map card.
                    seconds_since_idle_poll = 0
                    await self._fetch_and_update_position()
                    _LOGGER.debug(
                        "Idle position refresh for mower %s", self._device_id
                    )
                else:
                    _LOGGER.debug(
                        "Mower %s is not mowing; next idle refresh in %ds",
                        self._device_id,
                        _IDLE_POLL_INTERVAL - seconds_since_idle_poll,
                    )

                # Keepalive: re-activate the stream every 8 minutes regardless
                # of mowing state so it is ready the moment mowing resumes.
                if seconds_since_keepalive >= _KEEPALIVE_INTERVAL:
                    seconds_since_keepalive = 0
                    await self._activate_position_stream()

        except asyncio.CancelledError:
            _LOGGER.debug("Position tracking stopped for mower %s", self._device_id)
