"""Diagnostics download for Echo Button entries."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

TO_REDACT = {CONF_ADDRESS, "address", "serial"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data.client
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "button": {
                "address": client.address,
                "serial": client.serial,
                "connected": client.connected,
                "led_base_color": client.base_color,
                "last_status": client.last_status.hex(" ") if client.last_status else None,
                "capabilities": client.capabilities.hex(" ") if client.capabilities else None,
            },
        },
        TO_REDACT,
    )
