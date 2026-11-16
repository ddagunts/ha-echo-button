"""Press / long-press events from the Echo Button, for automations."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EVENT_LONG_PRESS, EVENT_PRESS
from .entity import EchoButtonEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EchoButtonEvent(entry)])


class EchoButtonEvent(EchoButtonEntity, EventEntity):
    _attr_translation_key = "button"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [EVENT_PRESS, EVENT_LONG_PRESS]

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "button")

    @callback
    def _handle_client_event(self, event: str) -> None:
        if event in self._attr_event_types:
            self._trigger_event(event)
            self.async_write_ha_state()
