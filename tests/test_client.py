"""Client logic tests: press detection and LED sending, without Bluetooth."""

import asyncio
import socket

import pytest
from echo_button.client import EVENT_LONG_PRESS, EVENT_PRESS, EchoButtonClient, NotConnectedError
from echo_button.protocol import (
    MSG_BUTTON,
    MSG_SET_LIGHT,
    MSG_STATUS,
    FrameParser,
    LightStep,
    encode_message,
    parse_message,
)

SERIAL = "G000AB0000000000"
PRESSED = bytes.fromhex("0000010201000400000000")
RELEASED = bytes.fromhex("0000010301000400000000")


def feed(client: EchoButtonClient, msg_type: int, data: bytes, at: float) -> None:
    client._handle(parse_message(encode_message(msg_type, SERIAL, data)), at)


def make_client() -> tuple[EchoButtonClient, list[str]]:
    client = EchoButtonClient("aa:bb:cc:dd:ee:ff", link=None)
    events: list[str] = []
    client.add_listener(events.append)
    return client, events


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
        client._cancel_timer("_refresh_timer")
        ours.close()
        button.close()

    asyncio.run(scenario())
