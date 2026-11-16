"""Shared BlueZ D-Bus link used by all Echo Button clients. No Home Assistant imports.

The gadget channel itself is a direct RFCOMM connection (see rfcomm.py); this
link covers what only BlueZ knows: where a button's pairing lives (which
adapter), whether its Bluetooth link is up, and when its device object goes
away (adapter unplugged, bluetoothd restarted), so its client can look it up
again. If the system bus connection itself drops, the link reconnects.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Protocol

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from . import rfcomm
from .bluez import (
    BLUEZ,
    BLUEZ_ERRORS,
    CALL_TIMEOUT,
    DEVICE,
    adapter_address,
    add_match,
    dbus_call,
    find_device_path,
    first_adapter,
    unwrap,
)
from .protocol import GADGET_RFCOMM_CHANNEL

_LOGGER = logging.getLogger(__name__)

RECONNECT_SECONDS = 10.0
_MATCHES = (
    # A button's baseband link going up or down.
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',"
    "arg0='org.bluez.Device1'",
    # A button's device object going away (adapter unplugged, device removed).
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager',member='InterfacesRemoved'",
    # bluetoothd stopping or (re)starting.
    "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'",
)


class LinkListener(Protocol):
    def on_link_changed(self, connected: bool) -> None: ...
    def on_device_gone(self) -> None: ...


class BluezLink:
    def __init__(self) -> None:
        self.bus: MessageBus | None = None
        self._listeners: dict[str, LinkListener] = {}
        self._watcher: asyncio.Task | None = None

    async def start(self) -> None:
        await self._connect()

    async def stop(self) -> None:
        bus, self.bus = self.bus, None
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        if bus is not None:
            bus.disconnect()

    async def _connect(self) -> None:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            bus.add_message_handler(self._on_message)
            for rule in _MATCHES:
                await add_match(bus, rule)
        except BaseException:
            bus.disconnect()
            raise
        self.bus = bus
        self._watcher = self._spawn(self._watch(bus))

    async def _watch(self, bus: MessageBus) -> None:
        """Reconnect when the system bus connection drops (dbus-daemon restart, socket error)."""
        try:
            await bus.wait_for_disconnect()
            reason = "closed"
        except Exception as err:
            reason = str(err) or type(err).__name__
        if self.bus is not bus:
            return  # stop() closed it
        _LOGGER.warning("Lost the D-Bus connection (%s); reconnecting", reason)
        self.bus = None
        self._devices_gone()
        while True:
            await asyncio.sleep(RECONNECT_SECONDS)
            try:
                await self._connect()
            except BLUEZ_ERRORS as err:
                _LOGGER.debug("D-Bus reconnect failed: %s", err)
                continue
            _LOGGER.info("Reconnected to D-Bus")
            return

    def _spawn(self, coro: Coroutine) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(coro)

    async def call(self, path, interface, member, signature="", body=None, destination=BLUEZ, timeout=CALL_TIMEOUT):
        return await dbus_call(self.bus, path, interface, member, signature, body, destination, timeout)

    async def _objects(self) -> dict:
        """GetManagedObjects, with property values still wrapped (only what's needed gets unwrapped)."""
        return (await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]

    async def adapter_path(self) -> str:
        return first_adapter(await self._objects())

    async def find_device(self, address: str) -> tuple[str, str]:
        """(device object path, adapter address) for the button, on whichever adapter holds its pairing."""
        objects = await self._objects()
        path = find_device_path(objects, address)
        return path, adapter_address(objects, path)

    async def device_connected(self, dev_path: str) -> bool:
        try:
            body = await self.call(dev_path, "org.freedesktop.DBus.Properties", "Get", "ss", [DEVICE, "Connected"])
        except DBusError:
            return False
        return bool(unwrap(body[0]))

    async def open_channel(self, address: str, adapter: str) -> int:
        """Open the button's gadget channel; returns a connected non-blocking fd (caller closes it)."""
        return await rfcomm.connect(address, GADGET_RFCOMM_CHANNEL, adapter)

    def attach(self, dev_path: str, listener: LinkListener):
        self._listeners[dev_path] = listener
        return lambda: self._listeners.pop(dev_path, None)

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "PropertiesChanged":
            listener = self._listeners.get(msg.path)  # cheap check first: BLE scanning makes many of these
            if listener is not None and msg.body[0] == DEVICE and "Connected" in msg.body[1]:
                listener.on_link_changed(bool(unwrap(msg.body[1]["Connected"])))
        elif msg.member == "InterfacesRemoved":
            listener = self._listeners.get(msg.body[0])
            if listener is not None and DEVICE in msg.body[1]:
                listener.on_device_gone()
        elif msg.member == "NameOwnerChanged" and msg.body[0] == BLUEZ:
            # bluetoothd stopped or (re)started: its device objects are gone.
            self._devices_gone()

    def _devices_gone(self) -> None:
        for listener in list(self._listeners.values()):
            listener.on_device_gone()
