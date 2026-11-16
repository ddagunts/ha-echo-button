"""Keeps a connection to one Echo Button, reports presses, drives its LED. No Home Assistant imports."""

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine

from dbus_fast.errors import DBusError

from .bluez import BluezError, device_path
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

LONG_PRESS_SECONDS = 1.0
STALE_PRESS_SECONDS = 30.0  # a "pressed" with no "released" this long is dropped
CONNECT_TIMEOUT = 25.0
RETRY_WHILE_LINKED = 2.0  # Bluetooth link up but gadget channel closed
RETRY_FALLBACK = 120.0  # button asleep: mostly wait for it to reconnect to us
# One command lasts at most 255 x 65.5 s (~4.6 h); resend the steady light well before that.
BASE_LIGHT_REFRESH_SECONDS = 3600.0
OFF: Color = (0, 0, 0)


class NotConnectedError(Exception):
    """The button is not connected right now."""


class EchoButtonClient:
    """Run `run()` as a task. Listeners get EVENT_* strings, called in the event loop."""

    def __init__(self, address: str, link: BluezLink, logger: logging.Logger | None = None) -> None:
        self.address = address.upper()
        self.serial: str | None = None
        self.connected = False
        self.last_status: bytes | None = None  # type 0x03 data, meaning unknown (shown in diagnostics)
        self.capabilities: bytes | None = None  # type 0x04 data
        self._link = link
        self._log = logger or _LOGGER
        self._listeners: list[Callable[[str], None]] = []
        self._wake = asyncio.Event()
        self._linked = False  # BlueZ Device1.Connected (baseband link)
        self._fds: asyncio.Queue[int] = asyncio.Queue()
        self._chunks: asyncio.Queue[bytes] | None = None
        self._fd: int | None = None
        self._tx_seq = 1
        self._serial_seen = asyncio.Event()
        self._pressed_at: float | None = None
        self._dev_path: str | None = None
        self._base_color: Color | None = None  # steady LED color, None = off
        self._restore_timer: asyncio.TimerHandle | None = None
        self._refresh_timer: asyncio.TimerHandle | None = None
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
        self._cancel_timer("_restore_timer")
        await self._send(MSG_SET_LIGHT, data)
        if self._base_color is not None:
            seconds = sum(step.duration_ms for step in steps) * repeat / 1000
            self._restore_timer = asyncio.get_running_loop().call_later(
                seconds, lambda: self._spawn(self._apply_base_quietly()))

    async def set_base_light(self, color: Color | None) -> None:
        """Set the steady LED color (None = off). Kept across reconnects; sent now if connected."""
        self._base_color = None if color == OFF else color
        self._cancel_timer("_restore_timer")
        if self._fd is not None:
            await self._apply_base()

    async def _apply_base(self) -> None:
        self._cancel_timer("_refresh_timer")
        if self._base_color is None:
            await self._send(MSG_SET_LIGHT, encode_set_light([LightStep(1, OFF)]))
            return
        await self._send(MSG_SET_LIGHT, encode_set_light([LightStep(MAX_STEP_MS, self._base_color)], MAX_REPEAT))
        self._refresh_timer = asyncio.get_running_loop().call_later(
            BASE_LIGHT_REFRESH_SECONDS, lambda: self._spawn(self._apply_base_quietly()))

    async def _apply_base_quietly(self) -> None:
        try:
            await self._apply_base()
        except (NotConnectedError, OSError, TimeoutError) as err:
            self._log.debug("Echo Button %s: could not restore LED: %s", self.address, err)

    def _cancel_timer(self, name: str) -> None:
        if (timer := getattr(self, name)) is not None:
            timer.cancel()
            setattr(self, name, None)

    async def _send(self, msg_type: int, data: bytes) -> None:
        if self._fd is None:
            raise NotConnectedError(f"Echo Button {self.address} is not connected")
        if self.serial is None:
            await asyncio.wait_for(self._serial_seen.wait(), 2)
        seq = self._tx_seq
        self._tx_seq = (self._tx_seq + 1) & 0xFF
        while self._tx_seq in (STX, ETX, ESC):
            self._tx_seq = (self._tx_seq + 1) & 0xFF
        frame = encode_frame(encode_message(msg_type, self.serial, data), seq)
        for _ in range(100):  # frames are tiny; the socket buffer is practically never full
            try:
                os.write(self._fd, frame)
                return
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except TypeError as err:  # fd went away while waiting
                raise NotConnectedError(str(err)) from err
        raise OSError("Echo Button send buffer stayed full")

    # --- LinkListener (called by BluezLink in the event loop) ---

    def on_link_changed(self, connected: bool) -> None:
        self._linked = connected
        self._log.debug("Echo Button %s link %s", self.address, "up" if connected else "down")
        if connected:
            self._wake.set()  # the button woke up and connected to us: open the gadget channel now

    def on_channel_opened(self, fd: int) -> None:
        self._fds.put_nowait(fd)

    def on_channel_close_requested(self) -> None:
        if self._chunks is not None:
            self._chunks.put_nowait(b"")

    # --- main loop ---

    async def run(self) -> None:
        """Run until cancelled."""
        try:
            while True:
                try:
                    await self._run_once()
                except (BluezError, DBusError, OSError, EOFError) as err:
                    self._log.warning("Echo Button %s: Bluetooth problem (%s); retrying in 30 s", self.address, err)
                    await asyncio.sleep(30)
        finally:
            self._cancel_timer("_restore_timer")
            self._cancel_timer("_refresh_timer")
            for task in list(self._tasks):
                task.cancel()

    async def _run_once(self) -> None:
        self._dev_path = device_path(await self._link.adapter_path(), self.address)
        detach = self._link.attach(self._dev_path, self)
        try:
            self._linked = await self._link.device_connected(self._dev_path)
            while True:
                self._wake.clear()
                try:
                    await self._session()
                except (DBusError, OSError, TimeoutError) as err:
                    self._log.debug("Echo Button %s not reachable: %s", self.address, err)
                delay = RETRY_WHILE_LINKED if self._linked else RETRY_FALLBACK
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    pass
        finally:
            detach()
            self._set_connected(False)

    async def _session(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._fds.empty():
            os.close(self._fds.get_nowait())
        try:
            await asyncio.wait_for(self._link.connect_channel(self._dev_path), CONNECT_TIMEOUT)
        except DBusError as err:
            if err.type == "org.bluez.Error.AlreadyConnected":
                # BlueZ still holds a channel we no longer read from; drop it and retry.
                await self._link.disconnect_channel(self._dev_path)
            raise
        fd = await asyncio.wait_for(self._fds.get(), 5)

        chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._chunks = chunks

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
        self._set_connected(True)
        if self._base_color is not None:
            self._spawn(self._apply_base_quietly())  # waits for the serial in the connect burst
        parser = FrameParser()
        try:
            while data := await chunks.get():
                for payload in parser.feed(data):
                    if (message := parse_message(payload)) is not None:
                        self._handle(message, loop.time())
            self._log.info("Echo Button %s disconnected", self.address)
        finally:
            self._fd = None
            loop.remove_reader(fd)
            os.close(fd)
            self._chunks = None
            self._pressed_at = None
            self._cancel_timer("_restore_timer")
            self._cancel_timer("_refresh_timer")
            self._set_connected(False)

    def _handle(self, message: GadgetMessage, now: float) -> None:
        if message.serial:
            self.serial = message.serial
            self._serial_seen.set()
        if message.type == MSG_STATUS:
            self.last_status = message.data
        elif message.type == MSG_CAPABILITIES:
            self.capabilities = message.data
        if message.type != MSG_BUTTON:
            self._log.debug("Echo Button %s message type %#04x data %s", self.address, message.type,
                            message.data.hex(" "))
            return
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
