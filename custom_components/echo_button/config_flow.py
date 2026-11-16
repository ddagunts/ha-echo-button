"""Config flow: pick an already-paired Echo Button, or pair a new one."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    ColorRGBSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from . import bluez
from .const import (
    CONF_FEEDBACK_MS,
    CONF_OFF_COLOR,
    CONF_ON_COLOR,
    DEFAULT_FEEDBACK_MS,
    DEFAULT_OFF_COLOR,
    DEFAULT_ON_COLOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PAIR_NEW = "pair_new"


class EchoButtonConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EchoButtonOptionsFlow:
        return EchoButtonOptionsFlow()

    def __init__(self) -> None:
        self._known: dict[str, str] = {}
        self._pair_task: asyncio.Task[tuple[str, str]] | None = None
        self._paired: tuple[str, str] | None = None
        self._error: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == PAIR_NEW:
                return await self.async_step_pair()
            return await self._create_entry(address, self._known[address])

        try:
            buttons = await bluez.paired_buttons()
        except bluez.BLUEZ_ERRORS as err:
            _LOGGER.warning("Cannot reach BlueZ: %s", err)
            return self.async_abort(reason="bluez_unavailable")
        configured = self._async_current_ids()
        self._known = {address: name for address, name in buttons if address not in configured}
        if not self._known:
            return await self.async_step_pair()

        options = {address: f"{name} ({address})" for address, name in self._known.items()}
        options[PAIR_NEW] = "Pair a new button"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)}),
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pair_task is None:
            if user_input is None:
                errors = {"base": self._error} if self._error else None
                return self.async_show_form(step_id="pair", errors=errors)
            self._error = None
            self._pair_task = self.hass.async_create_task(bluez.pair_new_button())

        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair", progress_action="pairing", progress_task=self._pair_task
            )

        task, self._pair_task = self._pair_task, None
        try:
            self._paired = task.result()
        except bluez.ButtonNotFoundError:
            self._error = "not_found"
        except bluez.PairingFailedError as err:
            _LOGGER.warning("Echo Button pairing failed: %s", err)
            self._error = "pair_failed"
        except bluez.BLUEZ_ERRORS as err:
            _LOGGER.warning("Cannot reach BlueZ: %s", err)
            self._error = "bluez_unavailable"
        return self.async_show_progress_done(next_step_id="finish" if self._paired else "pair")

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._paired is not None
        return await self._create_entry(*self._paired)

    async def _create_entry(self, address: str, name: str) -> ConfigFlowResult:
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=name, data={CONF_ADDRESS: address})


class EchoButtonOptionsFlow(OptionsFlow):
    """Press-feedback colors and duration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_FEEDBACK_MS] = int(user_input[CONF_FEEDBACK_MS])
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_FEEDBACK_MS, default=options.get(CONF_FEEDBACK_MS, DEFAULT_FEEDBACK_MS)):
                NumberSelector(NumberSelectorConfig(min=0, max=10000, step=100, unit_of_measurement="ms",
                                                    mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_ON_COLOR, default=options.get(CONF_ON_COLOR, DEFAULT_ON_COLOR)): ColorRGBSelector(),
            vol.Required(CONF_OFF_COLOR, default=options.get(CONF_OFF_COLOR, DEFAULT_OFF_COLOR)): ColorRGBSelector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
