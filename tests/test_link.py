"""BlueZ helper and link tests, without D-Bus or Bluetooth."""

import asyncio

import pytest
from dbus_fast import Message, MessageType, Variant
from echo_button.bluez import (
    ADAPTER,
    DEVICE,
    DeviceNotFoundError,
    NoAdapterError,
    dbus_call,
    find_device_path,
    first_adapter,
)
from echo_button.link import BluezLink

ADDRESS = "AA:BB:CC:DD:EE:FF"
HCI0 = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
HCI1 = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


def device(paired: bool, address: str = ADDRESS) -> dict:
    return {DEVICE: {"Address": Variant("s", address), "Paired": Variant("b", paired)}}


def test_device_is_found_on_the_adapter_that_paired_it():
    objects = {
        "/org/bluez/hci0": {ADAPTER: {}},
        "/org/bluez/hci1": {ADAPTER: {}},
        HCI0: device(paired=False),  # merely seen by the first adapter
        HCI1: device(paired=True),
        "/org/bluez/hci0/dev_11_22_33_44_55_66": device(paired=True, address="11:22:33:44:55:66"),
    }
    assert find_device_path(objects, ADDRESS.lower()) == HCI1
    assert first_adapter(objects) == "/org/bluez/hci0"


def test_missing_device_and_missing_adapter_are_told_apart():
    with pytest.raises(DeviceNotFoundError):
        find_device_path({"/org/bluez/hci0": {ADAPTER: {}}}, ADDRESS)
    with pytest.raises(NoAdapterError):
        find_device_path({}, ADDRESS)


def test_calls_on_a_lost_bus_fail_instead_of_waiting_forever():
    class LostBus:
        connected = False

    async def scenario():
        with pytest.raises(OSError):
            await dbus_call(LostBus(), "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        with pytest.raises(OSError):
            await dbus_call(None, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")

    asyncio.run(scenario())


class Listener:
    def __init__(self) -> None:
        self.events: list = []

    def on_link_changed(self, connected: bool) -> None:
        self.events.append(("link", connected))

    def on_device_gone(self) -> None:
        self.events.append("gone")


def signal(path, interface, member, signature, body) -> Message:
    return Message(message_type=MessageType.SIGNAL, path=path, interface=interface, member=member,
                   signature=signature, body=body)


def test_link_routes_bluez_signals_to_the_buttons_client():
    link = BluezLink()
    ours, other = Listener(), Listener()
    link.attach(HCI1, ours)
    link.attach(HCI0, other)

    link._on_message(signal(HCI1, "org.freedesktop.DBus.Properties", "PropertiesChanged", "sa{sv}as",
                            [DEVICE, {"Connected": Variant("b", True)}, []]))
    link._on_message(signal(HCI1, "org.freedesktop.DBus.Properties", "PropertiesChanged", "sa{sv}as",
                            [DEVICE, {"RSSI": Variant("n", -60)}, []]))
    link._on_message(signal("/", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved", "oas",
                            [HCI1, [DEVICE, "org.freedesktop.DBus.Properties"]]))
    assert ours.events == [("link", True), "gone"]
    assert other.events == []

    # bluetoothd stopped: every button's device object is gone.
    link._on_message(signal("/org/freedesktop/DBus", "org.freedesktop.DBus", "NameOwnerChanged", "sss",
                            ["org.bluez", ":1.5", ""]))
    assert ours.events[-1] == "gone"
    assert other.events == ["gone"]


def test_adapter_address_comes_from_the_devices_parent_adapter():
    from echo_button.bluez import adapter_address

    objects = {
        "/org/bluez/hci0": {ADAPTER: {"Address": Variant("s", "A0:00:00:00:00:00")}},
        "/org/bluez/hci1": {ADAPTER: {"Address": Variant("s", "A1:11:11:11:11:11")}},
        HCI1: device(paired=True),
    }
    assert adapter_address(objects, HCI1) == "A1:11:11:11:11:11"
    with pytest.raises(NoAdapterError):
        adapter_address({HCI1: device(paired=True)}, HCI1)


def test_rfcomm_address_is_packed_like_sockaddr_rc():
    from echo_button.rfcomm import sockaddr_rc

    packed = bytes(sockaddr_rc("00:71:47:F9:9C:63", 4))
    assert packed[:2] == (31).to_bytes(2, "little")  # AF_BLUETOOTH
    assert packed[2:8] == bytes.fromhex("639cf9477100")  # bdaddr is little-endian
    assert packed[8] == 4
    with pytest.raises(ValueError):
        sockaddr_rc("00:71:47", 4)
