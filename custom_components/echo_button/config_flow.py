"""Config flow: pick an already-paired Echo Button, pair a new one, or add an AButton.

Buttons running AButton (the open firmware) are discovered over BLE by their service UUID
(in the scan response, so an ESPHome proxy must be in active mode) and need no pairing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.helpers.selector import (
    BooleanSelector,
    ColorRGBSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import abutton as A, bluez
from .const import (
    CONF_ADVERT_INTERVAL_MS,
    CONF_BOOT_BATTERY,
    CONF_BRIGHTNESS,
    CONF_FEEDBACK_MS,
    CONF_KEEP_AWAKE,
    CONF_KEEP_AWAKE_SECONDS,
    CONF_KIND,
    CONF_LONG_PRESS_MS,
    CONF_LOW_BATTERY_PCT,
    CONF_LOW_BATTERY_WARNING,
    CONF_MULTI_CLICK_MS,
    CONF_OFF_COLOR,
    CONF_ON_COLOR,
    CONF_PRESS_COLOR,
    CONF_RESTORE_LED,
    CONF_SLEEP,
    CONF_TX_POWER,
    DEFAULT_ABUTTON_PRESS_COLOR,
    DEFAULT_FEEDBACK_MS,
    DEFAULT_KEEP_AWAKE,
    DEFAULT_KEEP_AWAKE_SECONDS,
    DEFAULT_OFF_COLOR,
    DEFAULT_ON_COLOR,
    DEFAULT_PRESS_COLOR,
    DOMAIN,
    KIND_ABUTTON,
    TX_POWER_MAX,
)
from .protocol import SLEEP_SECONDS

_LOGGER = logging.getLogger(__name__)

PAIR_NEW = "pair_new"


class EchoButtonConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if config_entry.data.get(CONF_KIND) == KIND_ABUTTON:
            return AButtonOptionsFlow()
        return EchoButtonOptionsFlow()

    def __init__(self) -> None:
        self._abuttons: dict[str, str] = {}
        self._discovered: tuple[str, str] | None = None
        self._known: dict[str, str] = {}
        self._pair_task: asyncio.Task[tuple[str, str]] | None = None
        self._paired: tuple[str, str] | None = None
        self._error: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """An AButton seen over BLE. A stock entry for the same button becomes an AButton one."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured(updates={CONF_KIND: KIND_ABUTTON})
        self._discovered = (discovery_info.address, discovery_info.name or discovery_info.address)
        self.context["title_placeholders"] = {"name": self._discovered[1]}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovered is not None
        if user_input is not None:
            return await self._create_entry(*self._discovered, abutton=True)
        self._set_confirm_only()
        return self.async_show_form(step_id="bluetooth_confirm",
                                    description_placeholders={"name": self._discovered[1]})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == PAIR_NEW:
                return await self.async_step_pair()
            if address in self._abuttons:
                return await self._create_entry(address, self._abuttons[address], abutton=True)
            return await self._create_entry(address, self._known[address])

        configured = self._async_current_ids()
        self._abuttons = {
            info.address: info.name or info.address
            for info in async_discovered_service_info(self.hass, connectable=True)
            if A.SERVICE_UUID in info.service_uuids and info.address not in configured
        }
        try:
            buttons = await bluez.paired_buttons()
        except bluez.BLUEZ_ERRORS as err:
            _LOGGER.warning("Cannot reach BlueZ: %s", err)
            if not self._abuttons:
                return self.async_abort(reason="bluez_unavailable")
            buttons = []
        self._known = {address: name for address, name in buttons
                       if address not in configured and address not in self._abuttons}
        if not self._known and not self._abuttons:
            return await self.async_step_pair()

        options = {address: f"{name} ({address})" for address, name in self._known.items()}
        options.update({address: f"{name} ({address}, AButton)" for address, name in self._abuttons.items()})
        options[PAIR_NEW] = "Pair a new button (stock firmware)"
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

    async def _create_entry(self, address: str, name: str, abutton: bool = False) -> ConfigFlowResult:
        await self.async_set_unique_id(address, raise_on_progress=False)
        data = {CONF_ADDRESS: address}
        if abutton:
            data[CONF_KIND] = KIND_ABUTTON
            self._abort_if_unique_id_configured(updates={CONF_KIND: KIND_ABUTTON})
        else:
            self._abort_if_unique_id_configured()
        return self.async_create_entry(title=name, data=data)


class EchoButtonOptionsFlow(OptionsFlow):
    """Press flash color, press-feedback colors and duration, keep awake."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_FEEDBACK_MS] = int(user_input[CONF_FEEDBACK_MS])
            user_input[CONF_KEEP_AWAKE_SECONDS] = int(user_input[CONF_KEEP_AWAKE_SECONDS])
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_PRESS_COLOR, default=options.get(CONF_PRESS_COLOR, DEFAULT_PRESS_COLOR)):
                ColorRGBSelector(),
            vol.Required(CONF_FEEDBACK_MS, default=options.get(CONF_FEEDBACK_MS, DEFAULT_FEEDBACK_MS)):
                NumberSelector(NumberSelectorConfig(min=0, max=10000, step=100, unit_of_measurement="ms",
                                                    mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_ON_COLOR, default=options.get(CONF_ON_COLOR, DEFAULT_ON_COLOR)): ColorRGBSelector(),
            vol.Required(CONF_OFF_COLOR, default=options.get(CONF_OFF_COLOR, DEFAULT_OFF_COLOR)): ColorRGBSelector(),
            vol.Required(CONF_KEEP_AWAKE, default=options.get(CONF_KEEP_AWAKE, DEFAULT_KEEP_AWAKE)):
                BooleanSelector(),
            # Up to a minute short of the button's sleep timer, so a late request still makes it.
            vol.Required(CONF_KEEP_AWAKE_SECONDS,
                         default=options.get(CONF_KEEP_AWAKE_SECONDS, DEFAULT_KEEP_AWAKE_SECONDS)):
                NumberSelector(NumberSelectorConfig(min=10, max=SLEEP_SECONDS - 60, step=10, unit_of_measurement="s",
                                                    mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)


TX_POWER_OPTIONS = [SelectOptionDict(value=TX_POWER_MAX, label="Maximum (default)")] + [
    SelectOptionDict(value=str(dbm), label=f"{dbm:+d} dBm" if dbm else "0 dBm") for dbm in A.TX_POWER_STEPS[1:]]


class AButtonOptionsFlow(OptionsFlow):
    """Settings kept on an AButton: brightness, press color, click timing, adverts, battery."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_abutton()

    async def async_step_abutton(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_BRIGHTNESS, CONF_LONG_PRESS_MS, CONF_MULTI_CLICK_MS, CONF_ADVERT_INTERVAL_MS,
                        CONF_LOW_BATTERY_PCT):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        d = A.Config()

        def number(lo: int, hi: int, step: int, unit: str | None = None) -> NumberSelector:
            extra = {"unit_of_measurement": unit} if unit else {}
            return NumberSelector(NumberSelectorConfig(min=lo, max=hi, step=step, mode=NumberSelectorMode.BOX,
                                                       **extra))

        schema = vol.Schema({
            vol.Required(CONF_PRESS_COLOR, default=o.get(CONF_PRESS_COLOR, DEFAULT_ABUTTON_PRESS_COLOR)):
                ColorRGBSelector(),
            vol.Required(CONF_BRIGHTNESS, default=o.get(CONF_BRIGHTNESS, d.brightness)): number(1, 255, 1),
            vol.Required(CONF_LONG_PRESS_MS, default=o.get(CONF_LONG_PRESS_MS, d.long_press_ms)):
                number(300, 5000, 50, "ms"),
            vol.Required(CONF_MULTI_CLICK_MS, default=o.get(CONF_MULTI_CLICK_MS, d.multi_click_ms)):
                number(100, 1000, 10, "ms"),
            vol.Required(CONF_ADVERT_INTERVAL_MS, default=o.get(CONF_ADVERT_INTERVAL_MS, d.advert_interval_ms)):
                number(100, 10000, 50, "ms"),
            vol.Required(CONF_LOW_BATTERY_PCT, default=o.get(CONF_LOW_BATTERY_PCT, d.low_battery_pct)):
                number(0, 100, 1, "%"),
            vol.Required(CONF_LOW_BATTERY_WARNING, default=o.get(CONF_LOW_BATTERY_WARNING, True)): BooleanSelector(),
            vol.Required(CONF_BOOT_BATTERY, default=o.get(CONF_BOOT_BATTERY, True)): BooleanSelector(),
            vol.Required(CONF_RESTORE_LED, default=o.get(CONF_RESTORE_LED, True)): BooleanSelector(),
            vol.Required(CONF_TX_POWER, default=o.get(CONF_TX_POWER, TX_POWER_MAX)): SelectSelector(
                SelectSelectorConfig(options=TX_POWER_OPTIONS, mode=SelectSelectorMode.DROPDOWN)),
            vol.Required(CONF_SLEEP, default=o.get(CONF_SLEEP, False)): BooleanSelector(),
        })
        return self.async_show_form(step_id="abutton", data_schema=schema)
