"""Protocol tests. Data blocks are the ones the real button sent; the serial is made up."""

import pytest
from echo_button.protocol import (
    MSG_BUTTON,
    MSG_CAPABILITIES,
    MSG_SET_LIGHT,
    MSG_STATUS,
    STATE_PRESSED,
    STATE_RELEASED,
    FrameParser,
    LightStep,
    encode_frame,
    encode_message,
    encode_set_light,
    fade_steps,
    parse_color,
    parse_message,
)

SERIAL = "G000AB0000000000"
CAPABILITIES = bytes.fromhex("06 0002ff01 0005ff01 0006ff01 0007ff00 0009ff01 000aff01")
STATUS = bytes.fromhex("005b03")
PRESSED = bytes.fromhex("0000010201000400000000")
RELEASED = bytes.fromhex("0000010301000400000000")


def frame(msg_type: int, data: bytes, seq: int = 1) -> bytes:
    return encode_frame(encode_message(msg_type, SERIAL, data), seq)


def test_real_frame_from_button_decodes():
    # Captured verbatim from a button (serial replaced, checksum recomputed by hand).
    payload = encode_message(MSG_BUTTON, SERIAL, PRESSED)
    raw = bytes([0xF0, 0x02, 0x00, 0x0A]) + payload + (sum(payload) + 2).to_bytes(2, "big") + bytes([0xF1])
    assert FrameParser().feed(raw) == [payload]


def test_connect_burst_split_across_reads():
    burst = frame(MSG_CAPABILITIES, CAPABILITIES, 1) + frame(MSG_STATUS, STATUS, 2) + frame(MSG_BUTTON, PRESSED, 3)
    parser = FrameParser()
    payloads = parser.feed(burst[:37]) + parser.feed(burst[37:51]) + parser.feed(burst[51:])
    messages = [parse_message(p) for p in payloads]
    assert [m.type for m in messages] == [MSG_CAPABILITIES, MSG_STATUS, MSG_BUTTON]
    assert all(m.serial == SERIAL for m in messages)
    assert messages[0].data == CAPABILITIES
    assert messages[2].button_state == STATE_PRESSED


def test_release_state():
    message = parse_message(FrameParser().feed(frame(MSG_BUTTON, RELEASED))[0])
    assert message.button_state == STATE_RELEASED


def test_reserved_bytes_are_escaped_and_restored():
    payload = bytes([0x01, 0xF0, 0xF1, 0xF2, 0x41])
    raw = encode_frame(payload, 7)
    assert bytes([0xF2, 0x02, 0xF2, 0x03, 0xF2, 0x00]) in raw
    assert FrameParser().feed(raw) == [payload]


def test_bad_checksum_and_garbage_are_dropped():
    raw = bytearray(frame(MSG_BUTTON, PRESSED))
    raw[-2] ^= 0x01
    good = frame(MSG_STATUS, STATUS)
    assert FrameParser().feed(b"\x00\x13" + bytes(raw) + good) == [encode_message(MSG_STATUS, SERIAL, STATUS)]


def test_non_button_message_has_no_state():
    assert parse_message(encode_message(MSG_STATUS, SERIAL, STATUS)).button_state is None
    assert parse_message(b"\x02\x01") is None


@pytest.mark.parametrize(
    ("steps", "repeat", "expected"),
    [
        # Each of these was sent to a real button and confirmed by eye.
        ([LightStep(1000, (255, 0, 0)), LightStep(1000, (0, 255, 0))], 3, "0101030203e8ff00000003e800ff0000"),
        ([LightStep(1500, (255, 0, 0)), LightStep(1500, (0, 0, 255), True), LightStep(1500, (0, 0, 0), True)], 1,
         "0101010305dcff00000005dc0000ff0105dc00000001"),
        ([LightStep(0x6400, (0, 255, 0))], 1, "01010101640000ff0000"),
        ([LightStep(255, (255, 0, 0))], 1, "0101010100ffff000000"),
    ],
)
def test_set_light_encoding_matches_verified_payloads(steps, repeat, expected):
    assert encode_set_light(steps, repeat).hex() == expected


def test_set_light_envelope():
    payload = encode_message(MSG_SET_LIGHT, SERIAL, encode_set_light([LightStep(2000, (0, 255, 0))]))
    assert payload.hex() == "0106" + "0110" + SERIAL.encode().hex() + "010a" + "0101010107d000ff0000"


@pytest.mark.parametrize(
    ("steps", "repeat"),
    [
        ([], 1),
        ([LightStep(0, (1, 2, 3))], 1),
        ([LightStep(70000, (1, 2, 3))], 1),
        ([LightStep(10, (1, 2, 300))], 1),
        ([LightStep(10, (1, 2, 3))], 0),  # the button ignores repeat 0
        ([LightStep(10, (1, 2, 3))], 256),
        ([LightStep(10, (1, 2, 3))] * 42, 1),  # data length would not fit in one byte
    ],
)
def test_set_light_rejects_what_the_button_cannot_take(steps, repeat):
    with pytest.raises(ValueError):
        encode_set_light(steps, repeat)


def test_fade_steps():
    steps = fade_steps((255, 0, 0), (160, 0, 255), 1200)
    assert len(steps) == 16
    assert sum(s.duration_ms for s in steps) == 1200
    assert steps[-1].color == (160, 0, 255)
    assert not any(s.blend for s in steps)
    assert fade_steps((0, 0, 0), (9, 9, 9), 3, count=16)[-1].color == (9, 9, 9)  # short fades shrink the count


@pytest.mark.parametrize(("value", "color"), [
    ("#ff8800", (255, 136, 0)),
    (" FF8800 ", (255, 136, 0)),
    ([1, 2, 3], (1, 2, 3)),
    ((0, 0, 255), (0, 0, 255)),
    (["255", 0, 0], (255, 0, 0)),  # as Home Assistant's own byte validator allows
])
def test_parse_color(value, color):
    assert parse_color(value) == color


@pytest.mark.parametrize("value", [
    255, None, True, {"r": 1, "g": 2, "b": 3},
    "#fff", "#gg0000", "",
    [1, 2], [1, 2, 3, 4], [1, 2, 300], [-1, 0, 0], [None, 0, 0], ["x", 0, 0],
])
def test_parse_color_rejects_anything_else_with_a_value_error(value):
    with pytest.raises(ValueError):
        parse_color(value)


def test_message_with_an_unusable_serial_is_dropped():
    good = encode_message(MSG_STATUS, SERIAL, STATUS)
    assert parse_message(good) is not None
    for bad_serial in (b"G0\xff0AB0000000000", b"G000AB000000000\x00"):
        payload = bytes([0x01, MSG_STATUS, 0x01, len(bad_serial)]) + bad_serial + bytes([0x01, len(STATUS)]) + STATUS
        assert parse_message(payload) is None
