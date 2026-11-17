"""Keeps a connection to one Echo Button, reports presses, drives its LED. No Home Assistant imports."""

import asyncio
import logging
import math
import os
from collections.abc import Callable, Coroutine

from dbus_fast.errors import DBusError

from .bluez import BLUEZ_ERRORS
from .link import BluezLink
from .protocol import (
    ESC,
    ETX,
    MAX_REPEAT,
    MAX_STEP_MS,
    MSG_BUTTON,
    MSG_CAPABILITIES,
    MSG_SET_LIGHT,
    MSG_STATUS,
    MSG_STATUS_REQUEST,
    STATE_PRESSED,
    STATE_RELEASED,
    STX,
    Color,
    FrameParser,
    GadgetMessage,
    LightStep,
    encode_frame,
    encode_message,
    encode_set_light,
    parse_message,
)

_LOGGER = logging.getLogger(__name__)

EVENT_PRESS = "press"
EVENT_LONG_PRESS = "long_press"
EVENT_CONNECTION = "connection"  # check client.connected
EVENT_BATTERY = "battery"  # check client.battery

LONG_PRESS_SECONDS = 1.0
STALE_PRESS_SECONDS = 30.0  # a "pressed" with no "released" this long is dropped
CONNECT_TIMEOUT = 25.0
RETRY_WHILE_LINKED = 2.0  # Bluetooth link up but gadget channel closed
RETRY_WHILE_LINKED_MAX = 15.0  # ... doubling while it keeps failing, up to this
RETRY_FALLBACK = 120.0  # button asleep: mostly wait for it to reconnect to us
RETRY_SECONDS = 30.0  # BlueZ unreachable, or the button unknown to it
RELOOKUP_SECONDS = 2.0  # the button's device object went away; give BlueZ a moment, then look again
SEND_ATTEMPTS = 100  # x 10 ms; frames are tiny, so the socket buffer is practically never full
# One command lasts at most 255 x 65.5 s (~4.6 h); resend the steady light well before that.
BASE_LIGHT_REFRESH_SECONDS = 3600.0
# The battery reading sags while the LED is lit and takes a few seconds to recover
# (75 -> 52 at full red, back to 73-74 four seconds after going dark), so readings
# only count once the LED has been dark this long.
LED_SETTLE_SECONDS = 10.0
PRESS_FLASH_SECONDS = 2.0  # the built-in blue flash on every press; its exact length is unknown
OFF: Color = (0, 0, 0)


class NotConnectedError(Exception):
    """The button is not connected right now."""


class EchoButtonClient:
    """Run `run()` as a task. Listeners get EVENT_* strings, called in the event loop."""

    def __init__(self, address: str, link: BluezLink, logger: logging.Logger | None = None) -> None:
        self.address = address.upper()
        self.serial: str | None = None
        self.connected = False
        self.last_status: bytes | None = None  # type 0x03 data (shown in diagnostics)
        self.capabilities: bytes | None = None  # type 0x04 data
        self.battery: int | None = None  # last battery level read while the LED was dark
        self._link = link
        self._log = logger or _LOGGER
        self._listeners: list[Callable[[str], None]] = []
        self._wake = asyncio.Event()
        self._linked = False  # BlueZ Device1.Connected (baseband link)
        self._device_gone = False  # BlueZ dropped the device object: look the button up again
        self._retrying = False  # a lasting Bluetooth problem was already logged as a warning
        self._send_lock = asyncio.Lock()  # one frame goes out whole before the next starts
        self._fd: int | None = None
        self._tx_seq = 1
        self._serial_seen = asyncio.Event()
        self._pressed_at: float | None = None
        self._dev_path: str | None = None  # BlueZ device object, on the adapter holding the pairing
        self._adapter: str | None = None  # that adapter's Bluetooth address
        self._base_color: Color | None = None  # steady LED color, None = off
        self._base_timer: asyncio.TimerHandle | None = None  # sends the steady color again
        self._led_commands = 0  # LED commands sent so far; lets a late resend see it was overtaken
        # When the LED last went (or will go) dark, in loop time. A steady color counts as
        # lit regardless. Starts out as "long ago": a button that just woke up is dark.
        self._led_dark_at = -math.inf
        self._keep_awake_seconds: float | None = None  # None = off
        self._keeper: asyncio.Task | None = None  # sends the keep-awake requests while connected
        self._tasks: set[asyncio.Task] = set()

    def add_listener(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def _emit(self, event: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:  # a broken listener must not kill the connection
                self._log.exception("Echo Button listener failed")

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            self._emit(EVENT_CONNECTION)

    def _spawn(self, coro: Coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- LED ---

    @property
    def base_color(self) -> Color | None:
        return self._base_color

    async def play_animation(self, steps: list[LightStep], repeat: int = 1) -> None:
        """Play an animation now, then go back to the steady (base) light.

        Raises NotConnectedError if the button is not connected, ValueError if
        the animation can't be encoded.
        """
        data = encode_set_light(steps, repeat)
        await self._send(MSG_SET_LIGHT, data)
        seconds = sum(step.duration_ms for step in steps) * repeat / 1000
        self._led_dark_at = asyncio.get_running_loop().time() + seconds  # it replaces whatever was playing
        self._sent_light(seconds)

    async def set_base_light(self, color: Color | None) -> None:
        """Set the steady LED color (None = off). Kept across reconnects; sent now if connected."""
        self._base_color = None if color == OFF else color
        if self._fd is not None:
            await self._apply_base()

    async def _apply_base(self) -> None:
        if self._base_color is None:
            steps, repeat = [LightStep(1, OFF)], 1
        else:
            steps, repeat = [LightStep(MAX_STEP_MS, self._base_color)], MAX_REPEAT
        await self._send(MSG_SET_LIGHT, encode_set_light(steps, repeat))
        self._led_dark_at = asyncio.get_running_loop().time()  # off now, or lit by the steady color
        self._sent_light(BASE_LIGHT_REFRESH_SECONDS)

    def _sent_light(self, seconds: float) -> None:
        """After each LED command that went out: send the steady color again in `seconds`.

        Each command replaces whatever the LED was showing, so one timer covers both
        coming back after an animation and refreshing the steady color before it
        runs out. If a send fails, the timer of the command that is still playing
        stays as it was. With the steady light off there is nothing to resend: the
        LED goes dark by itself when an animation ends.
        """
        self._cancel_base_timer()
        self._led_commands += 1
        if self._base_color is not None:
            sent = self._led_commands
            self._base_timer = asyncio.get_running_loop().call_later(
                seconds, lambda: self._spawn(self._resend_base(sent)))

    def _restore_base_soon(self) -> None:
        """On (re)connect: send the steady color, unless another LED command gets there first."""
        if self._base_color is not None:
            self._spawn(self._resend_base(self._led_commands))

    async def _resend_base(self, sent: int) -> None:
        if sent != self._led_commands:
            return  # an animation went out since; its own timer brings the steady color back
        try:
            await self._apply_base()
        except (NotConnectedError, OSError, TimeoutError) as err:
            self._log.debug("Echo Button %s: could not restore LED: %s", self.address, err)

    def _cancel_base_timer(self) -> None:
        if self._base_timer is not None:
            self._base_timer.cancel()
            self._base_timer = None

    # --- keep awake ---

    @property
    def keep_awake_seconds(self) -> float | None:
        return self._keep_awake_seconds

    def set_keep_awake(self, seconds: float | None) -> None:
        """Send a status request every `seconds` while connected (None = off).

        The request has no visible effect, but keeps the button from falling asleep
        (protocol.SLEEP_SECONDS after the last one). One also goes out on connect.
        A change applies right away: while connected, a request goes out now and
        the interval starts over, so a longer interval can't leave a gap.
        """
        if seconds == self._keep_awake_seconds:
            return  # e.g. another option was saved
        if seconds is not None and seconds <= 0:
            raise ValueError("keep-awake interval must be positive")
        self._keep_awake_seconds = seconds
        if self._fd is not None:
            self._start_keeper()

    def _start_keeper(self) -> None:
        self._stop_keeper()
        if self._keep_awake_seconds is not None:
            self._keeper = asyncio.get_running_loop().create_task(self._keep_awake_loop(self._keep_awake_seconds))

    def _stop_keeper(self) -> None:
        if self._keeper is not None:
            self._keeper.cancel()
            self._keeper = None

    async def _keep_awake_loop(self, seconds: float) -> None:
        """Runs for one connection and interval. Starts with a request: connecting doesn't reset the sleep timer."""
        while True:
            try:
                await self._send(MSG_STATUS_REQUEST, b"")
            except (NotConnectedError, OSError, TimeoutError) as err:
                self._log.debug("Echo Button %s: keep-awake request failed: %s", self.address, err)
            await asyncio.sleep(seconds)

    async def _send(self, msg_type: int, data: bytes) -> None:
        if self._fd is None:
            raise NotConnectedError(f"Echo Button {self.address} is not connected")
        if self.serial is None:
            await asyncio.wait_for(self._serial_seen.wait(), 2)
        async with self._send_lock:
            fd = self._fd
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFF
            while self._tx_seq in (STX, ETX, ESC):
                self._tx_seq = (self._tx_seq + 1) & 0xFF
            rest = memoryview(encode_frame(encode_message(msg_type, self.serial, data), seq))
            for _ in range(SEND_ATTEMPTS):
                if fd is None or self._fd != fd:  # never write the rest of a frame into a newer connection
                    raise NotConnectedError(f"Echo Button {self.address} disconnected")
                try:
                    rest = rest[os.write(fd, rest):]
                except BlockingIOError:
                    pass
                if not rest:
                    return
                await asyncio.sleep(0.01)
        raise OSError("Echo Button send buffer stayed full")

    # --- LinkListener (called by BluezLink in the event loop) ---

    def on_link_changed(self, connected: bool) -> None:
        self._linked = connected
        self._log.debug("Echo Button %s link %s", self.address, "up" if connected else "down")
        if connected:
            self._wake.set()  # the button woke up and connected to us: open the gadget channel now

    def on_device_gone(self) -> None:
        self._log.debug("Echo Button %s: BlueZ device object gone; will look it up again", self.address)
        self._linked = False
        self._device_gone = True
        self._wake.set()

    # --- main loop ---

    async def run(self) -> None:
        """Run until cancelled."""
        try:
            while True:
                try:
                    await self._run_once()  # returns when BlueZ dropped the button's device object
                    await asyncio.sleep(RELOOKUP_SECONDS)
                except BLUEZ_ERRORS as err:
                    # Warn once; while the problem lasts (button unpaired, adapter unplugged), log at debug.
                    log = self._log.debug if self._retrying else self._log.warning
                    log("Echo Button %s: Bluetooth problem (%s); retrying in %.0f s", self.address, err, RETRY_SECONDS)
                    self._retrying = True
                    await asyncio.sleep(RETRY_SECONDS)
        finally:
            self._cancel_base_timer()
            for task in list(self._tasks):
                task.cancel()

    async def _run_once(self) -> None:
        self._device_gone = False
        self._dev_path, self._adapter = await self._link.find_device(self.address)
        detach = self._link.attach(self._dev_path, self)
        try:
            self._linked = await self._link.device_connected(self._dev_path)
            failures = 0  # gadget channel attempts that failed in a row
            while not self._device_gone:
                self._wake.clear()
                try:
                    await self._session()
                    failures = 0
                except (DBusError, OSError, TimeoutError) as err:
                    failures += 1
                    self._log.debug("Echo Button %s not reachable: %s", self.address, err or type(err).__name__)
                if self._device_gone:
                    break
                # Ask rather than rely on signals alone: a bluetoothd restart, for one, sends none.
                self._linked = await self._link.device_connected(self._dev_path)
                if self._linked:
                    # Link up but the channel won't open: back off 2, 4, 8 ... s.
                    delay = min(RETRY_WHILE_LINKED * 2 ** max(failures - 1, 0), RETRY_WHILE_LINKED_MAX)
                else:
                    delay = RETRY_FALLBACK
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    pass
        finally:
            detach()
            self._set_connected(False)

    async def _session(self) -> None:
        loop = asyncio.get_running_loop()
        fd = await asyncio.wait_for(self._link.open_channel(self.address, self._adapter), CONNECT_TIMEOUT)
        chunks: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable() -> None:
            try:
                data = os.read(fd, 1024)
            except BlockingIOError:
                return
            except OSError as err:
                self._log.debug("Echo Button %s read error: %s", self.address, err)
                data = b""
            if not data:
                loop.remove_reader(fd)
            chunks.put_nowait(data)

        os.set_blocking(fd, False)
        loop.add_reader(fd, on_readable)
        self._fd = fd
        self._log.info("Connected to Echo Button %s", self.address)
        self._retrying = False
        self._set_connected(True)
        if self.serial is not None:
            self._restore_base_soon()
        # else: commands need the button's serial, so _handle sends the steady color
        # once the button's first message brings it.
        self._start_keeper()
        parser = FrameParser()
        try:
            while data := await chunks.get():
                for payload in parser.feed(data):
                    if (message := parse_message(payload)) is not None:
                        self._handle(message, loop.time())
            self._log.info("Echo Button %s disconnected", self.address)
        finally:
            self._stop_keeper()
            self._fd = None
            loop.remove_reader(fd)
            os.close(fd)
            self._pressed_at = None
            self._cancel_base_timer()
            self._set_connected(False)

    def _handle(self, message: GadgetMessage, now: float) -> None:
        if message.serial:
            first = self.serial is None
            self.serial = message.serial
            self._serial_seen.set()
            if first and self._fd is not None:
                self._restore_base_soon()
        if message.type == MSG_STATUS:
            self.last_status = message.data
            if (level := message.battery) is not None:
                self._battery_reading(level, now)
        elif message.type == MSG_CAPABILITIES:
            self.capabilities = message.data
        if message.type != MSG_BUTTON:
            self._log.debug("Echo Button %s message type %#04x data %s", self.address, message.type,
                            message.data.hex(" "))
            return
        self._led_dark_at = max(self._led_dark_at, now + PRESS_FLASH_SECONDS)
        state = message.button_state
        if state == STATE_PRESSED:
            # Also sent once on connect without a matching release, so only
            # a release (below) produces an event.
            self._pressed_at = now
        elif state == STATE_RELEASED and self._pressed_at is not None:
            held = now - self._pressed_at
            self._pressed_at = None
            if held > STALE_PRESS_SECONDS:
                return
            event = EVENT_LONG_PRESS if held >= LONG_PRESS_SECONDS else EVENT_PRESS
            self._log.debug("Echo Button %s %s (held %.2f s)", self.address, event, held)
            self._emit(event)

    def _battery_reading(self, level: int, now: float) -> None:
        """Keep a battery reading only if the LED was dark and had settled when it was taken.

        With a steady LED color on all the time, the level only updates once it's turned off.
        """
        if self._base_color is not None or now < self._led_dark_at + LED_SETTLE_SECONDS:
            self._log.debug("Echo Button %s: battery %d read with the LED lit (or just after); not used",
                            self.address, level)
            return
        if level != self.battery:
            self.battery = level
            self._emit(EVENT_BATTERY)
