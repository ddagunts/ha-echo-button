"""Client logic tests: press detection and LED sending, without Bluetooth."""

import asyncio
import errno
import logging
import os
import socket

import echo_button.client
import pytest
from dbus_fast.errors import AuthError
from echo_button.bluez import DeviceNotFoundError
from echo_button.client import EVENT_LONG_PRESS, EVENT_PRESS, EchoButtonClient, NotConnectedError
from echo_button.protocol import (
    MAX_REPEAT,
    MAX_STEP_MS,
    MSG_BUTTON,
    MSG_SET_LIGHT,
    MSG_STATUS,
    FrameParser,
    LightStep,
    encode_message,
    encode_set_light,
    parse_message,
)

SERIAL = "G000AB0000000000"
STATUS = bytes.fromhex("005b03")
PRESSED = bytes.fromhex("0000010201000400000000")
RELEASED = bytes.fromhex("0000010301000400000000")

STEADY = (160, 0, 255)
STEADY_DATA = encode_set_light([LightStep(MAX_STEP_MS, STEADY)], MAX_REPEAT)
GREEN = [LightStep(50, (0, 255, 0))]
RED = [LightStep(300, (255, 0, 0))]


def feed(client: EchoButtonClient, msg_type: int, data: bytes, at: float) -> None:
    client._handle(parse_message(encode_message(msg_type, SERIAL, data)), at)


def make_client() -> tuple[EchoButtonClient, list[str]]:
    client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link=None)
    events: list[str] = []
    client.add_listener(events.append)
    return client, events


def connect(client: EchoButtonClient) -> tuple[socket.socket, socket.socket]:
    """Give the client a socket as if the gadget channel had just opened."""
    ours, button = socket.socketpair()
    ours.setblocking(False)
    button.setblocking(False)
    client._fd = ours.fileno()
    return ours, button


def sent(button: socket.socket) -> list[bytes]:
    """The LED command data blocks the button received since the last call."""
    try:
        frames = FrameParser().feed(button.recv(65536))
    except BlockingIOError:
        return []
    messages = [parse_message(p) for p in frames]
    assert all(m.type == MSG_SET_LIGHT and m.serial == SERIAL for m in messages)
    return [m.data for m in messages]


def test_press_and_long_press():
    client, events = make_client()
    feed(client, MSG_BUTTON, PRESSED, 0.0)  # the connect burst always contains a stray "pressed"
    feed(client, MSG_BUTTON, PRESSED, 5.0)
    feed(client, MSG_BUTTON, RELEASED, 5.2)
    feed(client, MSG_BUTTON, PRESSED, 9.0)
    feed(client, MSG_BUTTON, RELEASED, 10.7)
    assert events == [EVENT_PRESS, EVENT_LONG_PRESS]
    assert client.address == "AA:BB:CC:DD:EE:FF"
    assert client.serial == SERIAL


def test_unmatched_and_stale_releases_are_ignored():
    client, events = make_client()
    feed(client, MSG_BUTTON, RELEASED, 1.0)
    feed(client, MSG_BUTTON, PRESSED, 2.0)
    feed(client, MSG_BUTTON, RELEASED, 60.0)
    assert events == []


def test_status_is_kept_for_diagnostics():
    client, _ = make_client()
    feed(client, MSG_STATUS, bytes.fromhex("005b03"), 0.0)
    assert client.last_status == bytes.fromhex("005b03")


def test_led_commands_need_a_connection():
    async def scenario():
        client, _ = make_client()
        with pytest.raises(NotConnectedError):
            await client.play_animation([LightStep(100, (255, 0, 0))])
        await client.set_base_light((10, 20, 30))  # stored for the next connection, no error
        assert client.base_color == (10, 20, 30)

    asyncio.run(scenario())


def test_animation_then_restore_base_light():
    async def scenario():
        client, _ = make_client()
        ours, button = socket.socketpair()
        ours.setblocking(False)
        client._fd = ours.fileno()
        feed(client, MSG_STATUS, bytes.fromhex("005b03"), 0.0)  # learns the serial

        await client.set_base_light((160, 0, 255))
        await client.play_animation([LightStep(20, (0, 255, 0)), LightStep(20, (0, 0, 0))])
        await asyncio.sleep(0.15)  # the 40 ms animation ends, then the base light is resent

        button.setblocking(False)
        received = FrameParser().feed(button.recv(4096))
        messages = [parse_message(p) for p in received]
        assert [m.type for m in messages] == [MSG_SET_LIGHT] * 3
        assert [m.serial for m in messages] == [SERIAL] * 3
        assert messages[0].data.hex() == "0101ff01ffffa000ff00"  # steady purple, longest command
        assert messages[1].data.hex() == "010101020014" + "00ff00" + "00" + "0014" + "000000" + "00"
        assert messages[2].data == messages[0].data
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_refresh_does_not_cut_a_running_animation(monkeypatch):
    monkeypatch.setattr(echo_button.client, "BASE_LIGHT_REFRESH_SECONDS", 0.05)

    async def scenario():
        client, _ = make_client()
        ours, button = connect(client)
        feed(client, MSG_STATUS, STATUS, 0.0)
        await client.set_base_light(STEADY)  # refresh due in 50 ms
        await client.play_animation(RED)  # runs for 300 ms
        await asyncio.sleep(0.15)
        assert sent(button) == [STEADY_DATA, encode_set_light(RED)]
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_failed_send_keeps_the_running_animations_restore():
    async def scenario():
        client, _ = make_client()
        ours, button = connect(client)
        feed(client, MSG_STATUS, STATUS, 0.0)
        await client.set_base_light(STEADY)
        await client.play_animation(GREEN)  # 50 ms

        async def broken_send(msg_type: int, data: bytes) -> None:
            raise OSError("Echo Button send buffer stayed full")

        working_send, client._send = client._send, broken_send
        with pytest.raises(OSError):
            await client.play_animation(RED)
        client._send = working_send

        await asyncio.sleep(0.15)  # green ended: the steady color is back
        assert sent(button) == [STEADY_DATA, encode_set_light(GREEN), STEADY_DATA]
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_overlapping_animations_restore_after_the_last_one():
    async def scenario():
        client, _ = make_client()
        await client.set_base_light(STEADY)  # not connected yet: only stored
        ours, button = connect(client)
        # Both wait for the serial, which the button's first message brings.
        first = asyncio.create_task(client.play_animation(GREEN))
        second = asyncio.create_task(client.play_animation(RED))
        await asyncio.sleep(0)
        feed(client, MSG_STATUS, STATUS, 0.0)
        await asyncio.gather(first, second)

        await asyncio.sleep(0.15)  # green's end must not cut red short
        assert sent(button) == [encode_set_light(GREEN), encode_set_light(RED)]
        await asyncio.sleep(0.3)  # red ended
        assert sent(button) == [STEADY_DATA]
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_steady_color_goes_out_once_the_serial_is_known():
    async def scenario():
        client, _ = make_client()
        await client.set_base_light(STEADY)  # restored at startup, before the button connected
        ours, button = connect(client)
        feed(client, MSG_STATUS, STATUS, 0.0)  # first message after the restart brings the serial
        await asyncio.sleep(0.01)
        assert sent(button) == [STEADY_DATA]
        feed(client, MSG_STATUS, STATUS, 1.0)  # later messages don't resend it
        await asyncio.sleep(0.01)
        assert sent(button) == []
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_bus_auth_error_is_retried_not_fatal():
    class BusDown:
        async def find_device(self, address: str) -> str:
            raise AuthError("connection closed during authentication")

    async def scenario():
        client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link=BusDown())
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.01)
        assert not task.done()  # logged, waiting to retry
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class FakeLink:
    """Stands in for BluezLink: the button is never reachable, every attempt is recorded."""

    def __init__(self, paths: list[str], connected: list[bool]) -> None:
        self.paths = paths  # what find_device returns, one per lookup (the last one repeats)
        self.connected = connected  # what device_connected returns, one per call (the last one repeats)
        self.attached: list[str] = []
        self.attempts = 0
        self.listener = None

    async def find_device(self, address: str) -> tuple[str, str]:
        path = self.paths.pop(0) if len(self.paths) > 1 else self.paths[0]
        return path, "11:22:33:44:55:66"

    def attach(self, dev_path, listener):
        self.attached.append(dev_path)
        self.listener = listener
        return lambda: None

    async def device_connected(self, dev_path: str) -> bool:
        return self.connected.pop(0) if len(self.connected) > 1 else self.connected[0]

    async def open_channel(self, address: str, adapter: str) -> int:
        self.attempts += 1
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")


HCI0 = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
HCI1 = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


def test_looks_the_button_up_again_when_its_device_object_goes_away(monkeypatch):
    monkeypatch.setattr(echo_button.client, "RELOOKUP_SECONDS", 0)

    async def scenario():
        link = FakeLink([HCI0, HCI1], [False])
        client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.01)
        assert link.attached == [HCI0]
        link.listener.on_device_gone()  # e.g. the USB adapter was unplugged and came back as hci1
        await asyncio.sleep(0.01)
        assert link.attached == [HCI0, HCI1]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_link_that_dropped_without_a_signal_is_retried_slowly(monkeypatch):
    monkeypatch.setattr(echo_button.client, "RETRY_WHILE_LINKED", 0.01)
    monkeypatch.setattr(echo_button.client, "RETRY_FALLBACK", 10)

    async def scenario():
        link = FakeLink([HCI0], [True, False])  # linked at first, then gone (no signal, e.g. bluetoothd restart)
        client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.2)
        assert link.attempts == 1  # not one every 10 ms
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_lasting_bluetooth_problem_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(echo_button.client, "RETRY_SECONDS", 0.01)

    class Unpaired:
        async def find_device(self, address: str) -> str:
            raise DeviceNotFoundError("BlueZ doesn't know AA:BB:CC:DD:EE:FF")

    async def scenario():
        client = EchoButtonClient("aa:bb:cc:dd:ee:ff", Unpaired())
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    caplog.set_level(logging.DEBUG)
    asyncio.run(scenario())
    problems = [r for r in caplog.records if "Bluetooth problem" in r.message]
    assert len(problems) > 2
    assert [r.levelno for r in problems[:2]] == [logging.WARNING, logging.DEBUG]


def test_partial_writes_still_send_whole_frames_in_order(monkeypatch):
    async def scenario():
        client, _ = make_client()
        ours, button = connect(client)
        feed(client, MSG_STATUS, STATUS, 0.0)
        real_write = os.write
        calls = 0

        def chunky_write(fd, data):  # 7 bytes at a time, with a full buffer every other call
            nonlocal calls
            if fd != ours.fileno():
                return real_write(fd, data)
            calls += 1
            if calls % 2 == 0:
                raise BlockingIOError
            return real_write(fd, bytes(data[:7]))

        monkeypatch.setattr(os, "write", chunky_write)
        await asyncio.gather(client.play_animation(GREEN), client.play_animation(RED))
        monkeypatch.undo()
        assert sent(button) == [encode_set_light(GREEN), encode_set_light(RED)]
        client._cancel_base_timer()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_send_stops_when_the_connection_changes_mid_frame(monkeypatch):
    async def scenario():
        client, _ = make_client()
        ours, button = connect(client)
        feed(client, MSG_STATUS, STATUS, 0.0)

        def stuck_write(fd, data):
            raise BlockingIOError

        monkeypatch.setattr(os, "write", stuck_write)
        task = asyncio.create_task(client.play_animation(GREEN))
        await asyncio.sleep(0.03)
        client._fd = None  # the session ended while the frame was waiting
        with pytest.raises(NotConnectedError):
            await task
        monkeypatch.undo()
        ours.close()
        button.close()

    asyncio.run(scenario())


def test_channel_refused_while_linked_backs_off(monkeypatch):
    monkeypatch.setattr(echo_button.client, "RETRY_WHILE_LINKED", 0.01)
    monkeypatch.setattr(echo_button.client, "RETRY_WHILE_LINKED_MAX", 0.04)

    async def scenario():
        link = FakeLink([HCI0], [True])  # link stays up, channel always refused
        client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.3)
        # 10, 20, 40, 40, 40 ... ms apart: about 9 attempts, not 30
        assert 5 <= link.attempts <= 12
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
