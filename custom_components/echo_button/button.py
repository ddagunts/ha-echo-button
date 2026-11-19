"""Identify button for Echo Buttons running AButton (blinks the LED white three times)."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .abutton_client import WRITE_ERRORS, AButtonUnavailableError
from .entity import EchoButtonEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AButtonIdentify(entry)])


class AButtonIdentify(EchoButtonEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.IDENTIFY

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "identify")

    async def async_press(self) -> None:
        try:
            await self._client.identify()
        except (AButtonUnavailableError, *WRITE_ERRORS) as err:
            raise HomeAssistantError(f"{self.entity_id}: could not reach the button: {err}") from err
