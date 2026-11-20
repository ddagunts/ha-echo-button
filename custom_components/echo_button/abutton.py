"""AButton (the open Echo Button firmware) GATT protocol. No Home Assistant imports.

Used by the Home Assistant integration and by firmware/abutton_ble.py. The firmware side is
firmware/abutton/abutton.c ("GATT protocol"). Every write fits in 20 bytes, the default ATT
MTU, so it works through any Bluetooth proxy.

Service 0e5a1000-a8b9-4c3b-9d1a-ab7ff0ab7700:
  LED      read  R G B <animation playing>
           write 00 stop the animation | 01 R G B steady color |
                 02 <repeat, 0 = forever> <n steps> [steps] | 03 [steps]
                 step = <ms u16 big-endian> R G B <fade 0/1>; plays once all n arrived
  Config   13 bytes, see Config
  Name     0..26 printable ASCII; empty = the default name
  Command  01 reboot | 02 restore defaults | 03 identify
  Key      read <BTHome encryption on>; write a 16-byte key (encrypt) or 00 (stop). Only
           within 60 s of a press on the button, else ATT error 0x08. Never readable.
           (0.3.0+)
"""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass, field, replace

Color = tuple[int, int, int]

SERVICE_UUID = "0e5a1000-a8b9-4c3b-9d1a-ab7ff0ab7700"
LED_UUID = "0e5a1001-a8b9-4c3b-9d1a-ab7ff0ab7700"
CONFIG_UUID = "0e5a1002-a8b9-4c3b-9d1a-ab7ff0ab7700"
NAME_UUID = "0e5a1003-a8b9-4c3b-9d1a-ab7ff0ab7700"
COMMAND_UUID = "0e5a1004-a8b9-4c3b-9d1a-ab7ff0ab7700"
KEY_UUID = "0e5a1005-a8b9-4c3b-9d1a-ab7ff0ab7700"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

DEFAULT_WRITE_SIZE = 20  # ATT MTU 23 - 3
MAX_STEPS = 40
MAX_STEP_MS = 0xFFFF
MAX_REPEAT = 255  # 0 = forever
NAME_MAX = 26

LED_STOP = 0x00
LED_COLOR = 0x01
LED_ANIM_START = 0x02
LED_ANIM_MORE = 0x03

CMD_REBOOT = 0x01
CMD_DEFAULTS = 0x02
CMD_IDENTIFY = 0x03

FLAG_LOW_BATTERY_WARNING = 0x01  # two red blinks after a press when the battery is low
FLAG_BOOT_BATTERY = 0x02         # the boot blink shows the battery: green / amber / red
FLAG_RESTORE_LED = 0x04          # the steady LED color survives a reboot
FLAGS_ALL = FLAG_LOW_BATTERY_WARNING | FLAG_BOOT_BATTERY | FLAG_RESTORE_LED

KEY_LEN = 16
KEY_OFF = b"\x00"
ATT_INSUFFICIENT_AUTHORIZATION = 0x08  # a key write without a recent press on the button
PRESS_HINT = "press the button once, then try again within 60 seconds"

CONFIG_VERSION = 1
_CONFIG = struct.Struct("<BBB3sHHHB")


def _check_color(color: Color) -> Color:
    color = tuple(color)
    if len(color) != 3 or not all(isinstance(c, int) and 0 <= c <= 255 for c in color):
        raise ValueError(f"not an RGB color: {color!r}")
    return color


@dataclass(frozen=True)
class Config:
    """The button's settings (kept in its NVRAM)."""

    flags: int = FLAGS_ALL
    brightness: int = 255                      # 1..255, scales every LED output
    press_color: Color = (255, 255, 255)       # LED while held; black = none
    long_press_ms: int = 1000                  # 300..5000
    multi_click_ms: int = 350                  # 100..1000
    advert_interval_ms: int = 250              # 100..10000, idle advert interval
    low_battery_pct: int = 20                  # 0..100, warn below this

    def __post_init__(self) -> None:
        checks = (
            (self.flags & ~FLAGS_ALL == 0, "flags"),
            (1 <= self.brightness <= 255, "brightness (1-255)"),
            (300 <= self.long_press_ms <= 5000, "long press (300-5000 ms)"),
            (100 <= self.multi_click_ms <= 1000, "multi-click window (100-1000 ms)"),
            (100 <= self.advert_interval_ms <= 10000, "advert interval (100-10000 ms)"),
            (0 <= self.low_battery_pct <= 100, "low battery level (0-100 %)"),
        )
        for ok, what in checks:
            if not ok:
                raise ValueError(f"invalid {what}")
        _check_color(self.press_color)

    def flag(self, bit: int) -> bool:
        return bool(self.flags & bit)

    def with_flag(self, bit: int, on: bool) -> Config:
        return replace(self, flags=(self.flags | bit) if on else (self.flags & ~bit))

    def encode(self) -> bytes:
        return _CONFIG.pack(CONFIG_VERSION, self.flags, self.brightness, bytes(self.press_color),
                            self.long_press_ms, self.multi_click_ms, self.advert_interval_ms,
                            self.low_battery_pct)

    @classmethod
    def decode(cls, data: bytes) -> Config:
        if len(data) != _CONFIG.size:
            raise ValueError(f"config is {len(data)} bytes, expected {_CONFIG.size}")
        version, flags, brightness, press, long_ms, multi_ms, adv_ms, low = _CONFIG.unpack(data)
        if version != CONFIG_VERSION:
            raise ValueError(f"config version {version}, this client knows {CONFIG_VERSION}")
        return cls(flags, brightness, tuple(press), long_ms, multi_ms, adv_ms, low)


@dataclass(frozen=True)
class Step:
    duration_ms: int
    color: Color
    fade: bool = False  # fade from the previous color into this one

    def encode(self) -> bytes:
        if not 1 <= self.duration_ms <= MAX_STEP_MS:
            raise ValueError(f"step duration must be 1-{MAX_STEP_MS} ms")
        return struct.pack(">H", self.duration_ms) + bytes(_check_color(self.color)) + bytes([int(self.fade)])


@dataclass(frozen=True)
class LedState:
    color: Color
    animating: bool

    @classmethod
    def decode(cls, data: bytes) -> LedState:
        if len(data) != 4:
            raise ValueError(f"LED state is {len(data)} bytes, expected 4")
        return cls(tuple(data[:3]), bool(data[3]))


def led_color(color: Color | None) -> bytes:
    """Steady color; None (or black) turns it off."""
    return bytes([LED_COLOR]) + bytes(_check_color(color or (0, 0, 0)))


def led_stop() -> bytes:
    return bytes([LED_STOP])


def led_animation(steps: list[Step], repeat: int = 1, write_size: int = DEFAULT_WRITE_SIZE) -> list[bytes]:
    """The writes that upload and start an animation (repeat 0 = forever)."""
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"an animation has 1-{MAX_STEPS} steps")
    if not 0 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be 0-{MAX_REPEAT} (0 = forever)")
    if write_size < 9:
        raise ValueError("write size too small for one step")
    encoded = [s.encode() for s in steps]
    writes: list[bytes] = []
    pdu = bytearray([LED_ANIM_START, repeat, len(steps)])
    for step in encoded:
        if len(pdu) + len(step) > write_size:
            writes.append(bytes(pdu))
            pdu = bytearray([LED_ANIM_MORE])
        pdu += step
    writes.append(bytes(pdu))
    return writes


def check_name(name: str) -> bytes:
    raw = name.encode("ascii", errors="strict") if name.isascii() else None
    if raw is None or len(raw) > NAME_MAX or not all(0x20 <= b <= 0x7E for b in raw):
        raise ValueError(f"a name is 0-{NAME_MAX} printable ASCII characters")
    return raw


def generate_key() -> bytes:
    """A new random BTHome encryption key."""
    return secrets.token_bytes(KEY_LEN)


def parse_key(text: str) -> bytes:
    """A BTHome key as Home Assistant shows it: 32 hex characters."""
    try:
        key = bytes.fromhex(text.strip())
    except ValueError:
        key = b""
    if len(key) != KEY_LEN:
        raise ValueError(f"a BTHome key is {2 * KEY_LEN} hex characters")
    if key == bytes(KEY_LEN):
        raise ValueError("the all-zero key is not allowed")
    return key


def encryption_on(data: bytes) -> bool:
    """Decode a read of the key characteristic."""
    if len(data) != 1:
        raise ValueError(f"key state is {len(data)} bytes, expected 1")
    return bool(data[0])


def parse_hex_color(text: str) -> Color:
    """'ff8800' or '#ff8800' -> (255, 136, 0)."""
    text = text.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a hex color: {text!r}")
    return tuple(bytes.fromhex(text))


@dataclass
class ButtonInfo:
    """What a client knows after reading the button."""

    firmware: str = ""
    name: str = ""
    config: Config = field(default_factory=Config)
    led: LedState | None = None
