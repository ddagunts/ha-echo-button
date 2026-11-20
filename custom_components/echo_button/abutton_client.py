"""Home Assistant side of an AButton (open-firmware Echo Button): LED, settings, commands.

Presses and battery come from the button's BTHome adverts (Home Assistant's BTHome
integration). This client only connects when there is something to write, through Home
Assistant's Bluetooth stack, so it works with local adapters and ESPHome Bluetooth proxies
(active mode). It stays connected for IDLE_SECONDS after the last write, so a burst of LED
changes uses one connection; the button keeps advertising BTHome while connected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import abutton as A, bluez

IDLE_SECONDS = 20
WRITE_ERRORS = (BleakError, TimeoutError, OSError)
STALE_CLASSIC = "br-connection-create-socket"  # BlueZ tried Classic: an old stock-firmware pairing


async def forget_stale_pairing(address: str, log: logging.Logger) -> bool:
    """Drop a stock-firmware Classic pairing for `address` from the local BlueZ, if any.

    Without it BlueZ connects over Classic and fails. Does nothing (False) when this host
    has no BlueZ, e.g. only ESPHome proxies.
    """
    try:
        removed = await bluez.forget_classic_pairing(address)
    except bluez.BLUEZ_ERRORS as err:
        log.debug("AButton %s: no local BlueZ to clean up (%s)", address, err)
        return False
    if removed:
        log.info("AButton %s: removed %d old Classic pairing record(s) from BlueZ", address, removed)
    return bool(removed)


class AButtonUnavailableError(Exception):
    """The button isn't in range of any connectable Bluetooth adapter or proxy."""


class AButtonClient:
    def __init__(self, hass: HomeAssistant, address: str, logger: logging.Logger) -> None:
        self.hass = hass
        self.address = address
        self._log = logger
        self._lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._idle: asyncio.TimerHandle | None = None
        self._config: A.Config | None = None        # pushed on every connect
        self._config_sent = False
        self._power: A.Power | None = None          # likewise
        self._power_sent = False
        self.firmware: str | None = None
        self.connected = False
        self._listeners: list[Callable[[str], None]] = []

    # The stock client's listener API, so the shared entity base class works unchanged.
    def add_listener(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    # ---- connection -----------------------------------------------------------------
    async def _establish(self) -> BleakClientWithServiceCache:
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise AButtonUnavailableError(f"{self.address} is not in range of a connectable adapter or proxy")
        return await establish_connection(
            BleakClientWithServiceCache, device, self.address,
            disconnected_callback=self._on_disconnect, max_attempts=3)

    async def _connect(self) -> BleakClientWithServiceCache:
        if self._client is not None and self._client.is_connected:
            return self._client
        client = await self._open()
        if client.services.get_service(A.SERVICE_UUID) is None:
            await client.disconnect()
            raise AButtonUnavailableError(f"{self.address} has no AButton service (stock firmware?)")
        try:
            self.firmware = bytes(await client.read_gatt_char(A.FIRMWARE_REVISION_UUID)).decode(errors="replace")
        except WRITE_ERRORS:
            pass
        if client.services.get_characteristic(A.POWER_UUID) is None:
            # A proxy (or BlueZ) can serve the GATT table it cached before an OTA update.
            self._log.debug("AButton %s: firmware %s, refreshing its cached GATT services",
                            self.address, self.firmware)
            await client.clear_cache()
            await client.disconnect()
            client = await self._open()
        self._client = client
        self.connected = True
        self._config_sent = False
        self._power_sent = False
        return client

    async def _open(self) -> BleakClientWithServiceCache:
        try:
            client = await self._establish()
        except BleakError as err:
            if STALE_CLASSIC not in str(err) or not await forget_stale_pairing(self.address, self._log):
                raise
            # BlueZ re-creates the device as BLE from its next advert (every 250 ms by default).
            await asyncio.sleep(3)
            client = await self._establish()
        return client

    def _on_disconnect(self, _client) -> None:
        self.connected = False
        self._client = None

    def _arm_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self._idle = self.hass.loop.call_later(IDLE_SECONDS, lambda: self.hass.async_create_task(self.disconnect()))

    async def disconnect(self) -> None:
        async with self._lock:
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            client, self._client = self._client, None
            self.connected = False
            if client is not None:
                try:
                    await client.disconnect()
                except WRITE_ERRORS:
                    pass

    async def _write(self, uuid: str, payloads: list[bytes]) -> None:
        async with self._lock:
            try:
                client = await self._connect()
                if self._config is not None and not self._config_sent:
                    await client.write_gatt_char(A.CONFIG_UUID, self._config.encode(), response=True)
                    self._config_sent = True
                if self._power is not None and not self._power_sent:
                    await client.write_gatt_char(A.POWER_UUID, self._power.encode(), response=True)
                    self._power_sent = True
                for payload in payloads:
                    await client.write_gatt_char(uuid, payload, response=True)
            except WRITE_ERRORS:
                self._client = None                 # reconnect next time
                self.connected = False
                raise
            finally:
                self._arm_idle()

    # ---- API ------------------------------------------------------------------------
    async def set_color(self, color: A.Color | None) -> None:
        """Steady color (None = off). Also stops a running animation."""
        await self._write(A.LED_UUID, [A.led_stop(), A.led_color(color)])

    async def play_animation(self, steps: list[A.Step], repeat: int = 1) -> None:
        await self._write(A.LED_UUID, A.led_animation(steps, repeat))

    async def identify(self) -> None:
        await self._write(A.COMMAND_UUID, [bytes([A.CMD_IDENTIFY])])

    def set_power(self, power: A.Power) -> None:
        """Remember the power settings; they go out with the next write (see set_config)."""
        self._power = power
        self._power_sent = False

    async def set_config(self, config: A.Config, push: bool = True) -> None:
        """Remember the settings (pushed on every connect); write them now if `push`."""
        self._config = config
        self._config_sent = False
        if push:
            await self._write(A.CONFIG_UUID, [])     # _write sends pending config and power first
