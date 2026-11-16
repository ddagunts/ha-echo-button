#!/usr/bin/env python3
"""Probe an Amazon Echo Button over Classic Bluetooth using BlueZ.

What it does, all logged with millisecond timestamps:
  * Registers a pairing agent that answers legacy PIN requests (default 0000).
  * Registers a Serial Port Profile (SPP) handler, so an RFCOMM serial link
    works whichever side opens it, and hex-dumps every byte received.
  * Runs discovery and prints every device seen (name, class, UUIDs, RSSI).
  * Auto-pairs the first device whose name starts with "EchoBtn", trusts it,
    dumps its SDP records, then opens the SPP link to it.
  * Logs every Connected/Paired/etc. change, so a press that only makes the
    button reconnect is still visible.

Run:  .venv/bin/python echobtn_probe.py [--pins 0000,1234] [--address AA:BB:..]
"""

import argparse
import asyncio
import datetime
import socket
import sys

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

BLUEZ = "org.bluez"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/abuttons/agent"
PROFILE_PATH = "/abuttons/spp"
NAME_PREFIX = "echobtn"


def log(*parts):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def unwrap(value):
    """Recursively turn dbus-fast Variants into plain Python values."""
    if isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


def hexdump(data: bytes) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    {off:04x}  {hx:<47}  {asc}")
    return "\n".join(lines)


class Agent(ServiceInterface):
    """Accepts every pairing request; answers PIN requests from a list."""

    def __init__(self, pins):
        super().__init__("org.bluez.Agent1")
        self.pins = pins
        self.pin_index = 0

    @property
    def current_pin(self):
        return self.pins[self.pin_index % len(self.pins)]

    @method()
    def Release(self):
        log("agent: Release")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        log(f"agent: RequestPinCode {device} -> {self.current_pin}")
        return self.current_pin

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        log(f"agent: DisplayPinCode {device} {pincode}")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        log(f"agent: RequestPasskey {device} -> 0")
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        log(f"agent: DisplayPasskey {device} {passkey:06d} entered={entered}")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        log(f"agent: RequestConfirmation {device} {passkey:06d} -> yes")

    @method()
    def RequestAuthorization(self, device: "o"):
        log(f"agent: RequestAuthorization {device} -> yes")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        log(f"agent: AuthorizeService {device} {uuid} -> yes")

    @method()
    def Cancel(self):
        log("agent: Cancel")


class SppProfile(ServiceInterface):
    """BlueZ hands us the RFCOMM socket here, whichever side connected."""

    def __init__(self, loop):
        super().__init__("org.bluez.Profile1")
        self.loop = loop

    @method()
    def Release(self):
        log("profile: Release")

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}"):
        props = unwrap(fd_properties)
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        log(f"profile: NewConnection {device} props={props} local={sock.getsockname()} peer={sock.getpeername()}")
        self.loop.create_task(read_serial(sock, device))

    @method()
    def RequestDisconnection(self, device: "o"):
        log(f"profile: RequestDisconnection {device}")


async def read_serial(sock, device):
    loop = asyncio.get_running_loop()
    total = 0
    try:
        while True:
            data = await loop.sock_recv(sock, 4096)
            if not data:
                log(f"serial {device}: closed by peer after {total} bytes")
                break
            total += len(data)
            note = ""
            if len(data) >= 40:
                b39 = data[39]
                note = {0xC0: "  <-- byte39=C0 (press?)", 0xC1: "  <-- byte39=C1 (release?)"}.get(b39, f"  byte39={b39:02x}")
            log(f"serial {device}: RX {len(data)} bytes{note}\n{hexdump(data)}")
    except OSError as e:
        log(f"serial {device}: error {e!r} after {total} bytes")
    finally:
        sock.close()


class Probe:
    def __init__(self, bus, agent, target_address):
        self.bus = bus
        self.agent = agent
        self.target_address = target_address.upper() if target_address else None
        self.adapter_path = None
        self.devices = {}        # path -> merged Device1 properties
        self.handled = set()     # device paths we've started pairing
        self.loop = asyncio.get_running_loop()

    async def call(self, path, interface, member, signature="", body=None, destination=BLUEZ):
        reply = await self.bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def set_prop(self, path, interface, name, variant):
        await self.call(path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                        [interface, name, variant])

    async def start(self):
        objects = (await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces and self.adapter_path is None:
                self.adapter_path = path
                a = unwrap(ifaces["org.bluez.Adapter1"])
                log(f"adapter {path} {a.get('Address')} powered={a.get('Powered')} pairable={a.get('Pairable')}")
        if not self.adapter_path:
            sys.exit("no Bluetooth adapter found")

        # Listen to all BlueZ signals before doing anything that triggers them.
        await self.call("/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                        ["type='signal',sender='org.bluez'"], destination="org.freedesktop.DBus")
        self.bus.add_message_handler(self.on_message)

        await self.set_prop(self.adapter_path, "org.bluez.Adapter1", "Powered", Variant("b", True))
        await self.set_prop(self.adapter_path, "org.bluez.Adapter1", "Pairable", Variant("b", True))

        await self.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                        [AGENT_PATH, "KeyboardDisplay"])
        await self.call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o", [AGENT_PATH])
        log(f"agent registered, PINs to try: {self.agent.pins}")

        # No Role given -> BlueZ both listens for incoming SPP and lets us connect out.
        await self.call("/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile", "osa{sv}", [
            PROFILE_PATH, SPP_UUID, {
                "Name": Variant("s", "abuttons SPP"),
                "RequireAuthentication": Variant("b", False),
                "RequireAuthorization": Variant("b", False),
            }])
        log("SPP profile registered (incoming and outgoing)")

        # Report already-known devices (e.g. paired on a previous run).
        for path, ifaces in objects.items():
            if "org.bluez.Device1" in ifaces:
                self.update_device(path, unwrap(ifaces["org.bluez.Device1"]), initial=True)

        await self.call(self.adapter_path, "org.bluez.Adapter1", "StartDiscovery")
        log("discovery started - put the button in pairing mode (hold until it glows orange)")

    def on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            path, ifaces = msg.body
            if "org.bluez.Device1" in ifaces:
                self.update_device(path, unwrap(ifaces["org.bluez.Device1"]), added=True)
        elif msg.member == "InterfacesRemoved":
            path, ifaces = msg.body
            if "org.bluez.Device1" in ifaces and path in self.devices:
                log(f"device removed {self.describe(path)}")
        elif msg.member == "PropertiesChanged" and msg.body[0] == "org.bluez.Device1":
            self.update_device(msg.path, unwrap(msg.body[1]))

    def describe(self, path):
        d = self.devices.get(path, {})
        return f"{d.get('Address', path)} '{d.get('Name', '')}'"

    def is_target(self, d):
        if self.target_address:
            return d.get("Address", "").upper() == self.target_address
        return str(d.get("Name", "")).lower().startswith(NAME_PREFIX)

    def update_device(self, path, props, added=False, initial=False):
        known = self.devices.setdefault(path, {})
        changed = {k: v for k, v in props.items() if known.get(k) != v}
        known.update(props)
        if not changed:
            return
        target = self.is_target(known)
        quiet = set(changed) <= {"RSSI", "ManufacturerData", "TxPower"}
        if added or initial:
            kind = "known" if initial else "found"
            cls = known.get("Class")
            log(f"{kind} {known.get('Address')} name={known.get('Name')!r} class="
                f"{'0x%06x' % cls if cls is not None else None} rssi={known.get('RSSI')} "
                f"paired={known.get('Paired')} uuids={known.get('UUIDs')}" + ("  <== TARGET" if target else ""))
        elif target or not quiet:
            log(f"change {self.describe(path)}: {changed}")
        if target and path not in self.handled:
            self.handled.add(path)
            self.loop.create_task(self.setup_target(path))

    async def setup_target(self, path):
        d = self.devices[path]
        addr = d.get("Address")
        log(f"*** target {addr} ({d.get('Name')}) - setting up")
        try:
            await self.call(self.adapter_path, "org.bluez.Adapter1", "StopDiscovery")
            log("discovery stopped")
        except DBusError as e:
            log(f"StopDiscovery: {e.type}")

        if not self.devices[path].get("Paired"):
            for attempt in range(len(self.agent.pins)):
                log(f"pairing {addr} with PIN {self.agent.current_pin} (attempt {attempt + 1})")
                try:
                    await self.call(path, "org.bluez.Device1", "Pair")
                    log(f"PAIRED {addr}")
                    break
                except DBusError as e:
                    log(f"pair failed: {e.type}: {e.text}")
                    if e.type == "org.bluez.Error.AlreadyExists":
                        break
                    self.agent.pin_index += 1
                    await asyncio.sleep(1)
            else:
                log("all PINs failed; keeping listener running anyway")

        try:
            await self.set_prop(path, "org.bluez.Device1", "Trusted", Variant("b", True))
            log(f"trusted {addr}")
        except DBusError as e:
            log(f"trust failed: {e.type}")

        proc = await asyncio.create_subprocess_exec(
            "sdptool", "records", addr,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        log(f"sdptool records {addr}:\n{out.decode(errors='replace')}")

        uuids = self.devices.get(path, {}).get("UUIDs") or []
        log(f"UUIDs now: {uuids}")
        if SPP_UUID in uuids:
            log(f"connecting SPP to {addr}")
            member, sig, body = "ConnectProfile", "s", [SPP_UUID]
        else:
            log(f"no SPP UUID advertised; asking BlueZ to connect all profiles of {addr}")
            member, sig, body = "Connect", "", []
        try:
            await self.call(path, "org.bluez.Device1", member, sig, body)
            log(f"{member} returned OK")
        except DBusError as e:
            log(f"{member} failed: {e.type}: {e.text}")
        log("now press the button a few times (single presses, a few seconds apart)")


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pins", default="0000,1234", help="comma-separated legacy PINs to try")
    ap.add_argument("--address", help="target this address instead of matching the name EchoBtn*")
    ap.add_argument("--duration", type=float, default=900, help="seconds to run (default 900)")
    args = ap.parse_args()

    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    agent = Agent(args.pins.split(","))
    bus.export(AGENT_PATH, agent)
    bus.export(PROFILE_PATH, SppProfile(asyncio.get_running_loop()))

    probe = Probe(bus, agent, args.address)
    await probe.start()
    try:
        await asyncio.sleep(args.duration)
    finally:
        log("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
