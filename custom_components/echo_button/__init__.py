"""Amazon Echo Button: use the 2017 Alexa game button as a Home Assistant toggle.

Talks to the button directly over Classic Bluetooth through the host's BlueZ,
so no Echo or Amazon account is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import voluptuous as vol
from dbus_fast.errors import DBusError

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.typing import ConfigType

from .bluez import BluezError
from .client import EchoButtonClient
from .const import DOMAIN, SERVICE_PLAY_ANIMATION
from .link import BluezLink
from .protocol import MAX_REPEAT, MAX_STEP_MS, MAX_STEPS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.LIGHT, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _color(value):
    """[r, g, b] or a hex string like '#ff8800'."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6:
            raise vol.Invalid("hex colors need 6 digits, like #ff8800")
        try:
            return tuple(bytes.fromhex(text))
        except ValueError as err:
            raise vol.Invalid(f"not a hex color: {value}") from err
    return tuple(vol.ExactSequence([cv.byte, cv.byte, cv.byte])(list(value)))


STEP_SCHEMA = vol.Schema({
    vol.Required("color"): _color,
    vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_STEP_MS)),
    vol.Optional("fade", default=False): cv.boolean,
})
PLAY_ANIMATION_SCHEMA = {
    vol.Required("steps"): vol.All(cv.ensure_list, [STEP_SCHEMA], vol.Length(min=1, max=MAX_STEPS)),
    vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_REPEAT)),
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


async def async_setup_entry(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> bool:
    shared: _Shared = hass.data.setdefault(DOMAIN, _Shared())
    async with shared.lock:
        if shared.link is None:
            link = BluezLink()
            try:
                await link.start()
                await link.adapter_path()
            except (BluezError, DBusError, OSError, EOFError) as err:
                await link.stop()
                raise ConfigEntryNotReady(f"Bluetooth (BlueZ) not available: {err}") from err
            shared.link = link
        shared.users += 1
        link = shared.link

    data = EchoButtonData(EchoButtonClient(entry.data[CONF_ADDRESS], link, _LOGGER))
    entry.runtime_data = data
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await _release_link(hass)
        raise
    data.task = hass.async_create_background_task(data.client.run(), f"echo_button {data.client.address}")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EchoButtonConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    data = entry.runtime_data
    if data.task is not None:
        data.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await data.task
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
