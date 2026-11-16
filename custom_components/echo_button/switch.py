"""Toggle switch flipped by each short press of the Echo Button."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .client import EVENT_PRESS, OFF, NotConnectedError
from .const import (
    CONF_FEEDBACK_MS,
    CONF_OFF_COLOR,
    CONF_ON_COLOR,
    DEFAULT_FEEDBACK_MS,
    DEFAULT_OFF_COLOR,
    DEFAULT_ON_COLOR,
)
from .entity import EchoButtonEntity
from .protocol import LightStep, fade_steps

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([EchoButtonToggle(entry)])


class EchoButtonToggle(EchoButtonEntity, SwitchEntity, RestoreEntity):
    _attr_translation_key = "toggle"
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "toggle")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    @callback
    def _handle_client_event(self, event: str) -> None:
        if event == EVENT_PRESS:
            self._attr_is_on = not self._attr_is_on
            self.async_write_ha_state()
            self._show_feedback()

    @callback
    def _show_feedback(self) -> None:
        """Glow the new state's color on the button, then fade back."""
        options = self._entry.options
        total_ms = int(options.get(CONF_FEEDBACK_MS, DEFAULT_FEEDBACK_MS))
        if total_ms <= 0:
            return
        if self._attr_is_on:
            color = tuple(options.get(CONF_ON_COLOR, DEFAULT_ON_COLOR))
        else:
            color = tuple(options.get(CONF_OFF_COLOR, DEFAULT_OFF_COLOR))
        hold_ms = max(1, total_ms * 4 // 10)
        fade_to = self._client.base_color or OFF  # fade into the LED entity's steady color, if any
        steps = [LightStep(hold_ms, color), *fade_steps(color, fade_to, max(1, total_ms - hold_ms))]
        self.hass.async_create_task(self._play(steps), eager_start=True)

    async def _play(self, steps: list[LightStep]) -> None:
        try:
            await self._client.play_animation(steps)
        except (NotConnectedError, OSError, TimeoutError) as err:
            _LOGGER.debug("No press feedback for %s: %s", self._client.address, err)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
