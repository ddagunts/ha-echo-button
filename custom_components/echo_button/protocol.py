"""Echo Button wire protocol (Alexa Gadgets RFCOMM channel 4). No Home Assistant imports.

Frame (same framing as Amazon's Alexa Gadgets Classic transport):
    F0 | 02 | 00 | seq | escaped(payload + checksum_hi + checksum_lo) | F1
    escape: F0/F1/F2 inside a frame are sent as F2, (F2 ^ byte)
    checksum = (sum(payload) + 0x02 + 0x00) & 0xFFFF, big-endian

Payload (Echo Button's pre-protobuf format, reverse-engineered):
    01 | type | 01 | len | serial (ASCII, 16 bytes) | 01 | len | data
Button -> host message types:
    0x01 button state. data[3]: 0x02 = pressed, 0x03 = released;
         data[8:11]: R G B the LED was showing at that moment
    0x03 status (on connect, every few minutes, and in reply to 0x07)
    0x04 capability list (on connect): commands 02 05 06 07 09 0a

Host -> button commands (the serial must be the button's own; the button
checks lengths strictly and silently ignores anything malformed):
    0x06 play light animation now:
         01 01 <repeat 1-255> <step count> + per step:
         <duration ms, u16 big-endian> <R> <G> <B> <blend: 01 = fade into this color>
         The button's built-in blue press flash still plays over it.
    0x07 status request (no data) -> replies with 0x03; also keeps the button awake
    0x02 0x05 0x09 0x0a: take data, meaning unknown

The button falls asleep SLEEP_SECONDS after the last status request (opening a
new connection doesn't reset that) and drops off; the press that wakes it is lost.
"""

from dataclasses import dataclass

STX, ETX, ESC = 0xF0, 0xF1, 0xF2
_CMD, _ERR = 0x02, 0x00

GADGET_RFCOMM_CHANNEL = 4

MSG_BUTTON = 0x01
MSG_STATUS = 0x03
MSG_CAPABILITIES = 0x04
MSG_SET_LIGHT = 0x06
MSG_STATUS_REQUEST = 0x07

STATE_PRESSED = 0x02
STATE_RELEASED = 0x03

MAX_STEPS = 41  # the data length field is one byte: 4 + 6 * 41 = 250
MAX_STEP_MS = 0xFFFF
MAX_REPEAT = 255

SLEEP_SECONDS = 600

Color = tuple[int, int, int]


@dataclass(frozen=True)
class LightStep:
    duration_ms: int
    color: Color
    blend: bool = False  # fade from the previous color into this one


@dataclass(frozen=True)
class GadgetMessage:
    type: int
    serial: str
    data: bytes

    @property
    def button_state(self) -> int | None:
        if self.type == MSG_BUTTON and len(self.data) >= 4:
            return self.data[3]
        return None


class FrameParser:
    """Turns the RFCOMM byte stream into checksum-verified frame payloads."""

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        payloads = []
        for byte in data:
            if byte == STX:
                self._buf, self._escaped = bytearray(), False
            elif self._buf is None:
                continue
            elif byte == ETX:
                payload = _check_frame(bytes(self._buf))
                if payload is not None:
                    payloads.append(payload)
                self._buf = None
            elif byte == ESC:
                self._escaped = True
            else:
                self._buf.append(ESC ^ byte if self._escaped else byte)
                self._escaped = False
        return payloads


def _check_frame(body: bytes) -> bytes | None:
    """body = cmd, err, seq, payload..., csum_hi, csum_lo. Returns payload if the checksum matches."""
    if len(body) < 5:
        return None
    cmd, err, payload = body[0], body[1], body[3:-2]
    checksum = body[-2] << 8 | body[-1]
    if (sum(payload) + cmd + err) & 0xFFFF != checksum:
        return None
    return payload


def encode_frame(payload: bytes, seq: int) -> bytes:
    checksum = (sum(payload) + _CMD + _ERR) & 0xFFFF
    body = bytearray()
    for byte in payload + bytes([checksum >> 8, checksum & 0xFF]):
        if byte in (STX, ETX, ESC):
            body += bytes([ESC, ESC ^ byte])
        else:
            body.append(byte)
    return bytes([STX, _CMD, _ERR, seq & 0xFF]) + bytes(body) + bytes([ETX])


def encode_message(msg_type: int, serial: str, data: bytes) -> bytes:
    serial_bytes = serial.encode("ascii")
    return (bytes([0x01, msg_type, 0x01, len(serial_bytes)]) + serial_bytes
            + bytes([0x01, len(data)]) + data)


def encode_set_light(steps: list[LightStep], repeat: int = 1) -> bytes:
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"an animation needs 1-{MAX_STEPS} steps")
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be 1-{MAX_REPEAT}")
    data = bytearray([0x01, 0x01, repeat, len(steps)])
    for step in steps:
        if not 1 <= step.duration_ms <= MAX_STEP_MS:
            raise ValueError(f"step duration must be 1-{MAX_STEP_MS} ms")
        if len(step.color) != 3 or not all(0 <= c <= 255 for c in step.color):
            raise ValueError("color must be three values 0-255")
        data += bytes([step.duration_ms >> 8, step.duration_ms & 0xFF, *step.color, 1 if step.blend else 0])
    return bytes(data)


def parse_color(value: object) -> Color:
    """A color written as [r, g, b] (0-255 each) or as a hex string like "#ff8800".

    Raises ValueError for anything else.
    """
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6:
            raise ValueError("hex colors need 6 digits, like #ff8800")
        try:
            return tuple(bytes.fromhex(text))
        except ValueError:
            raise ValueError(f"not a hex color: {value}") from None
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"a color is [r, g, b] or a hex string like #ff8800, not {value!r}")
    try:
        color = tuple(int(c) for c in value)
    except (TypeError, ValueError):
        raise ValueError(f"color values must be numbers 0-255, not {value!r}") from None
    if not all(0 <= c <= 255 for c in color):
        raise ValueError(f"color values must be 0-255, not {value!r}")
    return color


def fade_steps(start: Color, end: Color, duration_ms: int, count: int = 16) -> list[LightStep]:
    """A fade as fixed-color steps (excluding `start`, ending on `end`).

    Used instead of the button's own blend for press feedback: the built-in
    press flash plays over the start of the animation and throws the button's
    blend off (it fades from the wrong color).
    """
    count = max(1, min(count, duration_ms))
    steps = []
    for i in range(1, count + 1):
        t = i / count
        color = tuple(round(a + (b - a) * t) for a, b in zip(start, end, strict=True))
        ms = duration_ms * i // count - duration_ms * (i - 1) // count
        steps.append(LightStep(max(1, ms), color))
    return steps


def parse_message(payload: bytes) -> GadgetMessage | None:
    if len(payload) < 2 or payload[0] != 0x01:
        return None
    fields = []
    i = 2
    while i + 2 <= len(payload):
        length = payload[i + 1]
        fields.append(payload[i + 2:i + 2 + length])
        i += 2 + length
    if not fields:
        return None
    try:
        serial = fields[0].decode("ascii")
    except UnicodeDecodeError:
        return None  # replies must quote the serial, so one we can't encode is useless
    if not serial.isprintable():
        return None
    data = fields[1] if len(fields) > 1 else b""
    return GadgetMessage(type=payload[1], serial=serial, data=bytes(data))
