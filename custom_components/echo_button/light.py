"""The Echo Button's LED as a light entity, plus the play_animation action.

Stock buttons: over the Classic Bluetooth gadget channel. AButton: over BLE (abutton_client).
"""

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

from . import abutton as A
from .abutton_client import WRITE_ERRORS as ABUTTON_ERRORS, AButtonUnavailableError
from .client import NotConnectedError
from .const import CONF_KIND, KIND_ABUTTON
from .entity import EchoButtonEntity
from .protocol import MAX_STEPS, LightStep

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    abutton = entry.data.get(CONF_KIND) == KIND_ABUTTON
    async_add_entities([AButtonLed(entry) if abutton else EchoButtonLed(entry)])


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
        if repeat == 0:
            raise ServiceValidationError(f"{self.entity_id}: repeat 0 (until stopped) needs AButton firmware")
        if len(steps) > MAX_STEPS:
            raise ServiceValidationError(f"{self.entity_id}: at most {MAX_STEPS} steps")
        light_steps = [LightStep(step["duration"], step["color"], step["fade"]) for step in steps]
        try:
            await self._client.play_animation(light_steps, repeat)
        except NotConnectedError as err:
            raise HomeAssistantError(f"{self.entity_id}: the Echo Button is not connected") from err
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except (OSError, TimeoutError) as err:
            raise HomeAssistantError(f"{self.entity_id}: could not send the animation: {err}") from err


class AButtonLed(EchoButtonLed):
    """The LED of a button running AButton: steady color and animations over BLE.

    Animations play over the steady color and return to it; turning the light on or off
    stops a running animation. The button keeps the steady color across reboots (unless its
    restore-LED setting is off), so the state restored here matches it.
    """

    _restoring = False

    async def _apply(self) -> None:
        if self._restoring:
            return  # at startup: the button already shows its kept color; don't connect to every button
        try:
            await self._client.set_color(self._output_color() if self._attr_is_on else None)
        except (AButtonUnavailableError, *ABUTTON_ERRORS) as err:
            _LOGGER.debug("LED change for %s not delivered: %s", self._client.address, err)
            raise HomeAssistantError(f"{self.entity_id}: could not reach the button: {err}") from err

    async def async_added_to_hass(self) -> None:
        self._restoring = True
        try:
            await super().async_added_to_hass()
        finally:
            self._restoring = False

    async def async_play_animation(self, steps: list[dict[str, Any]], repeat: int = 1) -> None:
        if len(steps) > A.MAX_STEPS:
            raise ServiceValidationError(f"{self.entity_id}: at most {A.MAX_STEPS} steps")
        a_steps = [A.Step(step["duration"], tuple(step["color"]), step["fade"]) for step in steps]
        try:
            await self._client.play_animation(a_steps, repeat)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except (AButtonUnavailableError, *ABUTTON_ERRORS) as err:
            raise HomeAssistantError(f"{self.entity_id}: could not send the animation: {err}") from err
