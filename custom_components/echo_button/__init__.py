"""Amazon Echo Button: use the 2017 Alexa game button as a Home Assistant toggle.

Stock buttons: talks to the button directly over Classic Bluetooth through the host's
BlueZ, so no Echo or Amazon account is involved. Buttons running AButton (the open firmware
in firmware/): LED, settings and identify over BLE (abutton_client.py); their presses and
battery come through the BTHome integration.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import voluptuous as vol

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.typing import ConfigType

from .abutton import (
    FLAG_BOOT_BATTERY,
    FLAG_LOW_BATTERY_WARNING,
    FLAG_RESTORE_LED,
    Config as AConfig,
    Power as APower,
)
from .abutton_client import (
    WRITE_ERRORS as ABUTTON_ERRORS,
    AButtonClient,
    AButtonUnavailableError,
    forget_stale_pairing,
)
from .bluez import BLUEZ_ERRORS
from .client import EchoButtonClient
from .const import (
    CONF_ADVERT_INTERVAL_MS,
    CONF_BOOT_BATTERY,
    CONF_BRIGHTNESS,
    CONF_KEEP_AWAKE,
    CONF_KEEP_AWAKE_SECONDS,
    CONF_KIND,
    CONF_LONG_PRESS_MS,
    CONF_LOW_BATTERY_PCT,
    CONF_LOW_BATTERY_WARNING,
    CONF_MULTI_CLICK_MS,
    CONF_PRESS_COLOR,
    CONF_RESTORE_LED,
    CONF_SLEEP,
    CONF_TX_POWER,
    DEFAULT_ABUTTON_PRESS_COLOR,
    DEFAULT_KEEP_AWAKE,
    DEFAULT_KEEP_AWAKE_SECONDS,
    DEFAULT_PRESS_COLOR,
    DOMAIN,
    KIND_ABUTTON,
    SERVICE_PLAY_ANIMATION,
    TX_POWER_MAX,
)
from .link import BluezLink
from .protocol import MAX_REPEAT, MAX_STEP_MS, MAX_STEPS, parse_color

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.LIGHT, Platform.SENSOR, Platform.SWITCH]
ABUTTON_PLATFORMS = [Platform.BUTTON, Platform.LIGHT]


def is_abutton(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_KIND) == KIND_ABUTTON

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _color(value):
    """[r, g, b] or a hex string like '#ff8800'."""
    try:
        return parse_color(value)
    except ValueError as err:
        raise vol.Invalid(str(err)) from err


STEP_SCHEMA = vol.Schema({
    vol.Required("color"): _color,
    vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_STEP_MS)),
    vol.Optional("fade", default=False): cv.boolean,
})
PLAY_ANIMATION_SCHEMA = {
    vol.Required("steps"): vol.All(cv.ensure_list, [STEP_SCHEMA], vol.Length(min=1, max=MAX_STEPS)),
    # 0 = until stopped (AButton only)
    vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_REPEAT)),
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    service.async_register_platform_entity_service(
        hass, DOMAIN, SERVICE_PLAY_ANIMATION,
        entity_domain=LIGHT_DOMAIN, schema=PLAY_ANIMATION_SCHEMA, func="async_play_animation",
    )
    return True


@dataclass
class EchoButtonData:
    client: EchoButtonClient
    task: asyncio.Task | None = None


@dataclass
class _Shared:
    """One BlueZ link shared by all Echo Button entries."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    link: BluezLink | None = None
    users: int = 0


type EchoButtonConfigEntry = ConfigEntry[EchoButtonData]


@dataclass
class AButtonData:
    client: AButtonClient


async def async_setup_entry(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> bool:
    if is_abutton(entry):
        return await _async_setup_abutton(hass, entry)
    shared: _Shared = hass.data.setdefault(DOMAIN, _Shared())
    async with shared.lock:
        if shared.link is None:
            link = BluezLink()
            try:
                await link.start()
                await link.adapter_path()
            except BLUEZ_ERRORS as err:
                await link.stop()
                raise ConfigEntryNotReady(f"Bluetooth (BlueZ) not available: {err}") from err
            shared.link = link
        shared.users += 1
        link = shared.link

    data = EchoButtonData(EchoButtonClient(entry.data[CONF_ADDRESS], link, _LOGGER))
    entry.runtime_data = data
    _apply_options(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await _release_link(hass)
        raise
    # Options take effect on the running client (the switch reads its own on each
    # press), so saving them doesn't reload the entry and drop the connection.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    data.task = hass.async_create_background_task(data.client.run(), f"echo_button {data.client.address}")
    return True


def _apply_options(entry: EchoButtonConfigEntry) -> None:
    options = entry.options
    entry.runtime_data.client.set_press_color(tuple(options.get(CONF_PRESS_COLOR, DEFAULT_PRESS_COLOR)))
    enabled = options.get(CONF_KEEP_AWAKE, DEFAULT_KEEP_AWAKE)
    entry.runtime_data.client.set_keep_awake(
        float(options.get(CONF_KEEP_AWAKE_SECONDS, DEFAULT_KEEP_AWAKE_SECONDS)) if enabled else None)


async def _async_options_updated(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> None:
    _apply_options(entry)


def abutton_config(options) -> AConfig:
    """The button's settings from the entry options (defaults where unset)."""
    d = AConfig()
    flags = 0
    for key, bit in ((CONF_LOW_BATTERY_WARNING, FLAG_LOW_BATTERY_WARNING), (CONF_BOOT_BATTERY, FLAG_BOOT_BATTERY),
                     (CONF_RESTORE_LED, FLAG_RESTORE_LED)):
        if options.get(key, True):
            flags |= bit
    return AConfig(
        flags=flags,
        brightness=int(options.get(CONF_BRIGHTNESS, d.brightness)),
        press_color=tuple(options.get(CONF_PRESS_COLOR, DEFAULT_ABUTTON_PRESS_COLOR)),
        long_press_ms=int(options.get(CONF_LONG_PRESS_MS, d.long_press_ms)),
        multi_click_ms=int(options.get(CONF_MULTI_CLICK_MS, d.multi_click_ms)),
        advert_interval_ms=int(options.get(CONF_ADVERT_INTERVAL_MS, d.advert_interval_ms)),
        low_battery_pct=int(options.get(CONF_LOW_BATTERY_PCT, d.low_battery_pct)),
    )


def abutton_power(options) -> APower:
    """The button's transmit power and sleep from the entry options (defaults where unset)."""
    tx = options.get(CONF_TX_POWER, TX_POWER_MAX)
    return APower(tx_power=None if tx == TX_POWER_MAX else int(tx), sleep=bool(options.get(CONF_SLEEP, False)))


async def _async_setup_abutton(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> bool:
    # A button that ran the stock firmware may still be paired over Classic on this host,
    # which makes every BLE connection fail (br-connection-create-socket).
    await forget_stale_pairing(entry.data[CONF_ADDRESS], _LOGGER)
    client = AButtonClient(hass, entry.data[CONF_ADDRESS], _LOGGER)
    entry.runtime_data = AButtonData(client)
    # Sent on the next connection (the first LED change, identify, or an options change).
    client.set_power(abutton_power(entry.options))
    await client.set_config(abutton_config(entry.options), push=False)
    await hass.config_entries.async_forward_entry_setups(entry, ABUTTON_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_abutton_options_updated))
    return True


async def _async_abutton_options_updated(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> None:
    client: AButtonClient = entry.runtime_data.client
    client.set_power(abutton_power(entry.options))
    try:
        await client.set_config(abutton_config(entry.options))
    except (AButtonUnavailableError, *ABUTTON_ERRORS) as err:
        _LOGGER.warning("AButton %s: settings saved; they reach the button on its next connection (%s)",
                        client.address, err)


async def async_unload_entry(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> bool:
    if is_abutton(entry):
        if not await hass.config_entries.async_unload_platforms(entry, ABUTTON_PLATFORMS):
            return False
        await entry.runtime_data.client.disconnect()
        return True
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    data = entry.runtime_data
    if data.task is not None:
        data.task.cancel()
        try:
            await data.task
        except asyncio.CancelledError:
            pass
        except Exception:  # it had already died; still release the shared link below
            _LOGGER.exception("Echo Button %s: connection task had failed", data.client.address)
    await _release_link(hass)
    return True


async def _release_link(hass: HomeAssistant) -> None:
    """Drop one user of the shared BlueZ link; stop it when nobody uses it."""
    shared: _Shared = hass.data[DOMAIN]
    async with shared.lock:
        shared.users -= 1
        if shared.users == 0 and shared.link is not None:
            await shared.link.stop()
            shared.link = None
