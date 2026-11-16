"""Shared BlueZ link used by all Echo Button clients. No Home Assistant imports.

BlueZ opens each button's gadget channel (RFCOMM channel 4, advertised in SDP as
class 0x1201) and hands us the connected socket as a file descriptor through
org.bluez.Profile1.NewConnection. That way no AF_BLUETOOTH socket is created in
Python, which some Python builds (including some container images) lack.

One profile registration is shared by every button; connections are routed to
the right client by BlueZ device path.

Note: no `from __future__ import annotations` - dbus-fast reads the D-Bus
signatures from the method annotations of the profile class.
"""

import asyncio
import logging
import os
from typing import Protocol

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

from .bluez import ADAPTER, BLUEZ, DEVICE, NoAdapterError, unwrap

_LOGGER = logging.getLogger(__name__)

GADGET_UUID = "00001201-0000-1000-8000-00805f9b34fb"
_PROFILE_PATH = "/echo_button/gadget"


class LinkListener(Protocol):
    def on_link_changed(self, connected: bool) -> None: ...
    def on_channel_opened(self, fd: int) -> None: ...
    def on_channel_close_requested(self) -> None: ...


class _GadgetProfile(ServiceInterface):
    def __init__(self, link: "BluezLink") -> None:
        super().__init__("org.bluez.Profile1")
        self._link = link

    @method()
    def Release(self):
        _LOGGER.debug("BlueZ released the Echo Button profile")

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}"):
        self._link.dispatch_new_connection(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):
        self._link.dispatch_close_request(device)


class BluezLink:
    def __init__(self) -> None:
        self.bus: MessageBus | None = None
        self._listeners: dict[str, LinkListener] = {}
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        self.bus.export(_PROFILE_PATH, _GadgetProfile(self))
        self.bus.add_message_handler(self._on_message)
        await self._add_match("type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                              "member='PropertiesChanged'")
        await self._add_match("type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',"
                              "arg0='org.bluez'")
        await self._register_profile()

    async def stop(self) -> None:
        if self.bus is None:
            return
        try:
            await self.call("/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile", "o", [_PROFILE_PATH])
        except (DBusError, OSError, EOFError):
            pass
        self.bus.disconnect()
        self.bus = None

    async def call(self, path, interface, member, signature="", body=None, destination=BLUEZ):
        if self.bus is None:
            raise OSError("D-Bus link not started")
        reply = await self.bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def _add_match(self, rule: str) -> None:
        await self.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule],
                        destination="org.freedesktop.DBus")

    async def _register_profile(self) -> None:
        await self.call("/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile", "osa{sv}", [
            _PROFILE_PATH, GADGET_UUID, {
                "Name": Variant("s", "Echo Button gadget"),
                "Role": Variant("s", "client"),
                "AutoConnect": Variant("b", False),
            }])
        _LOGGER.debug("Registered Echo Button profile with BlueZ")

    async def adapter_path(self) -> str:
        objects = unwrap((await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0])
        for path, ifaces in sorted(objects.items()):
            if ADAPTER in ifaces:
                return path
        raise NoAdapterError("no Bluetooth adapter found")

    async def device_connected(self, dev_path: str) -> bool:
        try:
            body = await self.call(dev_path, "org.freedesktop.DBus.Properties", "Get", "ss", [DEVICE, "Connected"])
        except DBusError:
            return False
        return bool(unwrap(body[0]))

    async def connect_channel(self, dev_path: str) -> None:
        """Ask BlueZ to open the gadget channel; the fd arrives via LinkListener.on_channel_opened."""
        await self.call(dev_path, DEVICE, "ConnectProfile", "s", [GADGET_UUID])

    async def disconnect_channel(self, dev_path: str) -> None:
        await self.call(dev_path, DEVICE, "DisconnectProfile", "s", [GADGET_UUID])

    def attach(self, dev_path: str, listener: LinkListener):
        self._listeners[dev_path] = listener
        return lambda: self._listeners.pop(dev_path, None)

    def dispatch_new_connection(self, dev_path: str, fd: int) -> None:
        if (listener := self._listeners.get(dev_path)) is None:
            os.close(fd)
            return
        listener.on_channel_opened(fd)

    def dispatch_close_request(self, dev_path: str) -> None:
        if (listener := self._listeners.get(dev_path)) is not None:
            listener.on_channel_close_requested()

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "PropertiesChanged" and msg.body[0] == DEVICE:
            listener = self._listeners.get(msg.path)
            changed = unwrap(msg.body[1])
            if listener is not None and "Connected" in changed:
                listener.on_link_changed(bool(changed["Connected"]))
        elif msg.member == "NameOwnerChanged" and msg.body[0] == BLUEZ and msg.body[2]:
            # bluetoothd (re)started: its profile registrations are gone, register again.
            task = asyncio.get_running_loop().create_task(self._reregister())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _reregister(self) -> None:
        try:
            await self._register_profile()
            _LOGGER.info("BlueZ restarted; Echo Button profile registered again")
        except (DBusError, OSError) as err:
            _LOGGER.warning("Could not re-register Echo Button profile after BlueZ restart: %s", err)
