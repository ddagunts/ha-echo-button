"""Small BlueZ D-Bus helpers for pairing Echo Buttons. No Home Assistant imports.

Note: no `from __future__ import annotations` here - dbus-fast reads the D-Bus
signatures from the method annotations of the agent class.
"""

import asyncio
import logging

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import AuthError, DBusError, InvalidAddressError
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
NAME_PREFIX = "EchoBtn"
_AGENT_PATH = "/echo_button/agent"


class BluezError(Exception):
    """Base error."""


class NoAdapterError(BluezError):
    """No Bluetooth adapter found."""


class ButtonNotFoundError(BluezError):
    """No Echo Button in pairing mode was found."""


class PairingFailedError(BluezError):
    """BlueZ could not pair with the button."""


# BlueZ or the system bus can't be reached, or refused a call: worth retrying or
# reporting. dbus-fast's other errors (bad signatures, names, paths) are bugs.
# AuthError also covers a bus connection that drops during the handshake.
BLUEZ_ERRORS = (BluezError, DBusError, AuthError, InvalidAddressError, OSError, EOFError)


def unwrap(value):
    if isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


def device_path(adapter_path: str, address: str) -> str:
    return f"{adapter_path}/dev_{address.upper().replace(':', '_')}"


class Bluez:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    @classmethod
    async def connect(cls) -> "Bluez":
        return cls(await MessageBus(bus_type=BusType.SYSTEM).connect())

    def disconnect(self) -> None:
        self.bus.disconnect()

    async def call(self, path, interface, member, signature="", body=None, destination=BLUEZ):
        reply = await self.bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def add_match(self, rule: str) -> None:
        await self.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule],
                        destination="org.freedesktop.DBus")

    async def remove_match(self, rule: str) -> None:
        await self.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "RemoveMatch", "s", [rule],
                        destination="org.freedesktop.DBus")

    async def objects(self) -> dict:
        body = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return unwrap(body[0])

    async def adapter_path(self) -> str:
        for path, ifaces in sorted((await self.objects()).items()):
            if ADAPTER in ifaces:
                return path
        raise NoAdapterError("no Bluetooth adapter found")

    async def set_property(self, path, interface, name, variant) -> None:
        await self.call(path, "org.freedesktop.DBus.Properties", "Set", "ssv", [interface, name, variant])

    async def device_properties(self, path: str) -> dict | None:
        return (await self.objects()).get(path, {}).get(DEVICE)


async def paired_buttons() -> list[tuple[str, str]]:
    """(address, name) of Echo Buttons that BlueZ already has a pairing for."""
    bz = await Bluez.connect()
    try:
        return sorted(
            (dev["Address"], dev.get("Name", dev["Address"]))
            for ifaces in (await bz.objects()).values()
            if (dev := ifaces.get(DEVICE))
            and dev.get("Paired")
            and str(dev.get("Name", "")).startswith(NAME_PREFIX)
        )
    finally:
        bz.disconnect()


class _AutoAcceptAgent(ServiceInterface):
    """Echo Buttons use Just Works pairing; accept whatever BlueZ asks."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        pass

    @method()
    def RequestAuthorization(self, device: "o"):
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def Cancel(self):
        pass


async def pair_new_button(timeout: float = 45.0) -> tuple[str, str]:
    """Find an Echo Button in pairing mode (glowing orange), pair and trust it.

    Returns (address, name). If BlueZ still holds an old pairing for the button
    (e.g. it was reset), that pairing is removed and the button paired again.
    """
    loop = asyncio.get_running_loop()
    bz = await Bluez.connect()
    agent = _AutoAcceptAgent()
    match = "type='signal',sender='org.bluez'"
    discovering = agent_registered = False
    try:
        adapter = await bz.adapter_path()
        names: dict[str, str] = {
            path: str(dev.get("Name", ""))
            for path, ifaces in (await bz.objects()).items()
            if (dev := ifaces.get(DEVICE))
        }
        candidates: asyncio.Queue[str] = asyncio.Queue()

        def on_message(msg: Message) -> None:
            if msg.message_type != MessageType.SIGNAL:
                return
            if msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                if DEVICE in ifaces:
                    names[path] = str(unwrap(ifaces[DEVICE]).get("Name", ""))
                    if names[path].startswith(NAME_PREFIX):
                        candidates.put_nowait(path)
            elif msg.member == "PropertiesChanged" and msg.body[0] == DEVICE:
                changed = unwrap(msg.body[1])
                if "Name" in changed:
                    names[msg.path] = str(changed["Name"])
                # RSSI/Name updates only come from discovery, i.e. the button is discoverable now.
                if ("RSSI" in changed or "Name" in changed) and names.get(msg.path, "").startswith(NAME_PREFIX):
                    candidates.put_nowait(msg.path)

        bz.bus.add_message_handler(on_message)
        await bz.add_match(match)

        bz.bus.export(_AGENT_PATH, agent)
        await bz.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                      [_AGENT_PATH, "NoInputNoOutput"])
        agent_registered = True
        await bz.set_property(adapter, ADAPTER, "Pairable", Variant("b", True))
        await bz.call(adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}", [{"Transport": Variant("s", "bredr")}])
        await bz.call(adapter, ADAPTER, "StartDiscovery")
        discovering = True
        _LOGGER.debug("Discovering Echo Buttons for %.0f s", timeout)

        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ButtonNotFoundError("no Echo Button in pairing mode found")
            try:
                path = await asyncio.wait_for(candidates.get(), remaining)
            except TimeoutError as err:
                raise ButtonNotFoundError("no Echo Button in pairing mode found") from err
            dev = await bz.device_properties(path)
            if dev is None:
                continue
            if dev.get("Paired"):
                _LOGGER.info("Removing old pairing for %s so it can be paired again", dev["Address"])
                await bz.call(adapter, ADAPTER, "RemoveDevice", "o", [path])
                continue
            break

        await bz.call(adapter, ADAPTER, "StopDiscovery")
        discovering = False

        address, name = dev["Address"], str(dev.get("Name", dev["Address"]))
        _LOGGER.info("Pairing with %s (%s)", name, address)
        try:
            await asyncio.wait_for(bz.call(path, DEVICE, "Pair"), 30)
        except DBusError as err:
            if err.type != "org.bluez.Error.AlreadyExists":
                raise PairingFailedError(f"{err.type}: {err.text}") from err
        except TimeoutError as err:
            raise PairingFailedError("pairing timed out") from err
        await bz.set_property(path, DEVICE, "Trusted", Variant("b", True))
        return address, name
    finally:
        if discovering:
            try:
                await bz.call(adapter, ADAPTER, "StopDiscovery")
            except DBusError:
                pass
        if agent_registered:
            try:
                await bz.call("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", [_AGENT_PATH])
            except DBusError:
                pass
        bz.disconnect()
