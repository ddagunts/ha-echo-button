"""The Echo Button's LED as a light entity, plus the play_animation action."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import NotConnectedError
from .entity import EchoButtonEntity
from .protocol import LightStep

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EchoButtonLed(entry)])


class EchoButtonLed(EchoButtonEntity, LightEntity, RestoreEntity):
    """Steady LED color. Press feedback and animations temporarily override it."""

    _attr_translation_key = "led"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "led")
        self._attr_is_on = False
        self._attr_rgb_color = (255, 255, 255)
        self._attr_brightness = 255

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self._attr_is_on = True
            if (rgb := last.attributes.get(ATTR_RGB_COLOR)) is not None:
                self._attr_rgb_color = tuple(rgb)
            if (brightness := last.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = brightness
            await self._apply()  # stored now, sent once the button connects

    def _output_color(self) -> tuple[int, int, int]:
        scale = (self._attr_brightness or 0) / 255
        return tuple(round(c * scale) for c in self._attr_rgb_color)

    async def _apply(self) -> None:
        try:
            await self._client.set_base_light(self._output_color() if self._attr_is_on else None)
        except (NotConnectedError, OSError, TimeoutError) as err:
            _LOGGER.debug("LED change for %s will apply on reconnect: %s", self._client.address, err)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_RGB_COLOR in kwargs:
            self._attr_rgb_color = tuple(kwargs[ATTR_RGB_COLOR])
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        self._attr_is_on = True
        await self._apply()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        await self._apply()
        self.async_write_ha_state()

    async def async_play_animation(self, steps: list[dict[str, Any]], repeat: int = 1) -> None:
        """Handler for the echo_button.play_animation action."""
        light_steps = [LightStep(step["duration"], step["color"], step["fade"]) for step in steps]
        try:
            await self._client.play_animation(light_steps, repeat)
        except NotConnectedError as err:
            raise HomeAssistantError(f"{self.entity_id}: the Echo Button is not connected") from err
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except (OSError, TimeoutError) as err:
            raise HomeAssistantError(f"{self.entity_id}: could not send the animation: {err}") from err
