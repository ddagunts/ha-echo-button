"""Whether the Echo Button is currently connected."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EVENT_CONNECTION
from .entity import EchoButtonEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EchoButtonConnected(entry)])


class EchoButtonConnected(EchoButtonEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "connected")

    @property
    def is_on(self) -> bool:
        return self._client.connected

    @callback
    def _handle_client_event(self, event: str) -> None:
        if event == EVENT_CONNECTION:
            self.async_write_ha_state()
