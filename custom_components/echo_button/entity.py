"""Base entity for Echo Button entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import EchoButtonClient
from .const import DOMAIN


class EchoButtonEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._client: EchoButtonClient = entry.runtime_data.client
        address = self._client.address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="Amazon",
            model="Echo Button",
            name=entry.title,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._client.add_listener(self._handle_client_event))

    @callback
    def _handle_client_event(self, event: str) -> None:
        """Called in the event loop with client.EVENT_* values."""
