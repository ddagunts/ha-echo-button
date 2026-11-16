"""Small BlueZ D-Bus helpers for pairing Echo Buttons. No Home Assistant imports.

Note: no `from __future__ import annotations` here - dbus-fast reads the D-Bus
signatures from the method annotations of the agent class.
"""

import asyncio
import contextlib
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
CALL_TIMEOUT = 30.0  # dbus-fast has no call timeout of its own
PAIR_TIMEOUT = 30.0
CLEANUP_TIMEOUT = 5.0


class BluezError(Exception):
    """Base error."""


class NoAdapterError(BluezError):
    """No Bluetooth adapter found."""


class DeviceNotFoundError(BluezError):
    """BlueZ has no device object for this address (on any adapter)."""


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


async def dbus_call(bus: MessageBus | None, path, interface, member, signature="", body=None,
                    destination=BLUEZ, timeout=CALL_TIMEOUT):
    """Call a D-Bus method and return the reply body.

    Raises DBusError for an error reply, OSError when the bus connection is gone
    (dbus-fast would otherwise wait forever for the reply), TimeoutError.
    """
    if bus is None or not bus.connected:
        raise OSError("not connected to D-Bus")
    reply = await asyncio.wait_for(bus.call(Message(
        destination=destination, path=path, interface=interface,
        member=member, signature=signature, body=body or [])), timeout)
    if reply is None:
        raise OSError("D-Bus connection closed")
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
    return reply.body


async def add_match(bus: MessageBus | None, rule: str) -> None:
    await dbus_call(bus, "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule],
                    destination="org.freedesktop.DBus")


def first_adapter(objects: dict) -> str:
    """Path of the first Bluetooth adapter in a GetManagedObjects result."""
    for path in sorted(objects):
        if ADAPTER in objects[path]:
            return path
    raise NoAdapterError("no Bluetooth adapter found")


def find_device_path(objects: dict, address: str) -> str:
    """Path of the device object for `address` in a GetManagedObjects result.

    Each adapter that knows the address has its own object for it; the paired
    one wins, then the first adapter. Values may still be wrapped in Variants.
    """
    address = address.upper()
    found = sorted(
        (not unwrap(dev.get("Paired", False)), path)
        for path, ifaces in objects.items()
        if (dev := ifaces.get(DEVICE)) is not None and str(unwrap(dev.get("Address", ""))).upper() == address
    )
    if found:
        return found[0][1]
    first_adapter(objects)  # no adapter at all is the more useful error
    raise DeviceNotFoundError(f"BlueZ doesn't know {address}; is it still paired?")


def adapter_address(objects: dict, dev_path: str) -> str:
    """Bluetooth address of the adapter that owns the device object at `dev_path`."""
    adapter = objects.get(dev_path.rsplit("/", 1)[0], {}).get(ADAPTER)
    if adapter is None:
        raise NoAdapterError(f"no adapter for {dev_path}")
    return str(unwrap(adapter["Address"]))


class Bluez:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    @classmethod
    async def connect(cls) -> "Bluez":
        return cls(await MessageBus(bus_type=BusType.SYSTEM).connect())

    def disconnect(self) -> None:
        self.bus.disconnect()

    async def call(self, path, interface, member, signature="", body=None, destination=BLUEZ, timeout=CALL_TIMEOUT):
        return await dbus_call(self.bus, path, interface, member, signature, body, destination, timeout)

    async def add_match(self, rule: str) -> None:
        await add_match(self.bus, rule)

    async def objects(self) -> dict:
        body = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return unwrap(body[0])

    async def adapter_path(self) -> str:
        return first_adapter(await self.objects())

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
    discovering = agent_registered = restore_pairable = False
    pairing: str | None = None  # device path while a Pair call may still be running in BlueZ
    try:
        objects = await bz.objects()
        adapter = first_adapter(objects)
        names: dict[str, str] = {
            path: str(dev.get("Name", ""))
            for path, ifaces in objects.items()
            if (dev := ifaces.get(DEVICE))
        }
        # Devices that answered this discovery (they report an RSSI), i.e. are in pairing
        # mode now. A paired button that is merely connected or renamed is left alone.
        in_range: set[str] = set()
        candidates: asyncio.Queue[str] = asyncio.Queue()

        def consider(path: str) -> None:
            if names.get(path, "").startswith(NAME_PREFIX):
                _LOGGER.debug("Discovery: %s %s, %s", names[path], path,
                              "answering" if path in in_range else "not answering discovery")
                if path in in_range:
                    candidates.put_nowait(path)

        def on_message(msg: Message) -> None:
            if msg.message_type != MessageType.SIGNAL:
                return
            if msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                if DEVICE in ifaces:
                    dev = unwrap(ifaces[DEVICE])
                    names[path] = str(dev.get("Name", ""))
                    if "RSSI" in dev:
                        in_range.add(path)
                    consider(path)
            elif msg.member == "PropertiesChanged" and msg.body[0] == DEVICE:
                changed = unwrap(msg.body[1])
                if "Name" in changed:
                    names[msg.path] = str(changed["Name"])
                if "RSSI" in changed:
                    in_range.add(msg.path)
                if "RSSI" in changed or "Name" in changed:
                    consider(msg.path)

        bz.bus.add_message_handler(on_message)
        await bz.add_match(match)

        bz.bus.export(_AGENT_PATH, agent)
        await bz.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                      [_AGENT_PATH, "NoInputNoOutput"])
        agent_registered = True
        if not objects[adapter][ADAPTER].get("Pairable"):
            await bz.set_property(adapter, ADAPTER, "Pairable", Variant("b", True))
            restore_pairable = True
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
        pairing = path
        try:
            await bz.call(path, DEVICE, "Pair", timeout=PAIR_TIMEOUT)
        except DBusError as err:
            pairing = None  # BlueZ answered, so the attempt is over
            if err.type != "org.bluez.Error.AlreadyExists":
                raise PairingFailedError(f"{err.type}: {err.text}") from err
        except TimeoutError as err:
            raise PairingFailedError("pairing timed out") from err
        pairing = None
        await bz.set_property(path, DEVICE, "Trusted", Variant("b", True))
        return address, name
    finally:
        # Also runs when the config flow is closed mid-way (the task is cancelled).
        async def cleanup(*call) -> None:
            with contextlib.suppress(*BLUEZ_ERRORS):
                await bz.call(*call, timeout=CLEANUP_TIMEOUT)

        if pairing is not None:
            await cleanup(pairing, DEVICE, "CancelPairing")
        if discovering:
            await cleanup(adapter, ADAPTER, "StopDiscovery")
        if restore_pairable:
            await cleanup(adapter, "org.freedesktop.DBus.Properties", "Set", "ssv",
                          [ADAPTER, "Pairable", Variant("b", False)])
        if agent_registered:
            await cleanup("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", [_AGENT_PATH])
        bz.disconnect()
