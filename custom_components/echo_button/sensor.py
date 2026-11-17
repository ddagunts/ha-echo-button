"""Battery level reported by the Echo Button."""

from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import EVENT_BATTERY
from .entity import EchoButtonEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EchoButtonBattery(entry)])


class EchoButtonBattery(EchoButtonEntity, RestoreSensor):
    """The button's own battery reading, taken only while its LED is dark (see client.py).

    Readings arrive when the button connects, every 10 minutes, and with each keep-awake
    request. The last value is kept while the button sleeps and across restarts.
    """

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "battery")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._client.battery is not None:
            self._attr_native_value = self._client.battery
        elif (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value

    @callback
    def _handle_client_event(self, event: str) -> None:
        if event == EVENT_BATTERY:
            self._attr_native_value = self._client.battery
            self.async_write_ha_state()
