#!/usr/bin/env python3
"""LED showcase for an Echo Button, using the integration's own Bluetooth code.

While waiting, the button breathes white. Press it to start; each scene's
caption is printed as it plays. Good for recording a demo video.

  python3 tools/demo.py AA:BB:CC:DD:EE:FF            # wait for a press, then run
  python3 tools/demo.py AA:BB:CC:DD:EE:FF --now      # start immediately

The button must already be paired with BlueZ (tools/echobtn_cli.py pair), and
nothing else (e.g. Home Assistant) may be connected to it at the same time.
"""

import argparse
import asyncio
import random
import sys
import time
import types
from pathlib import Path

_pkg = types.ModuleType("echo_button")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button")]
sys.modules["echo_button"] = _pkg

from echo_button.client import EVENT_PRESS, EchoButtonClient  # noqa: E402
from echo_button.link import BluezLink  # noqa: E402
from echo_button.protocol import LightStep as S  # noqa: E402
from echo_button.protocol import fade_steps  # noqa: E402

RED, ORANGE, YELLOW = (255, 0, 0), (255, 90, 0), (255, 200, 0)
GREEN, CYAN, BLUE = (0, 255, 0), (0, 255, 200), (0, 0, 255)
PURPLE, WHITE, OFF = (160, 0, 255), (255, 255, 255), (0, 0, 0)
AMBER = (255, 140, 0)


def fire() -> list[S]:
    rng = random.Random(7)
    return [S(rng.randint(50, 130), (255, rng.randint(40, 120), rng.randint(0, 10))) for _ in range(36)]


# (caption, steps, repeat)
SCENES = [
    ("Solid colors", [S(700, c) for c in (RED, ORANGE, YELLOW, GREEN, CYAN, BLUE, PURPLE, WHITE)], 1),
    ("Smooth fades", [S(900, c, True) for c in (RED, YELLOW, GREEN, CYAN, BLUE, PURPLE, RED)], 2),
    ("Blink", [S(120, WHITE), S(120, OFF)], 8),
    ("Police strobe", [S(70, RED), S(50, OFF), S(70, RED), S(160, OFF),
                       S(70, BLUE), S(50, OFF), S(70, BLUE), S(160, OFF)], 4),
    ("Breathing", [S(1200, PURPLE, True), S(1200, OFF, True)], 3),
    ("Fire flicker", fire(), 1),
]


def caption(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)


def duration(steps: list[S], repeat: int) -> float:
    return sum(s.duration_ms for s in steps) * repeat / 1000


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address")
    ap.add_argument("--now", action="store_true", help="start without waiting for a press")
    ap.add_argument("--toggle-seconds", type=float, default=25, help="length of the press-to-toggle scene")
    args = ap.parse_args()

    link = BluezLink()
    await link.start()
    client = EchoButtonClient(args.address, link)
    presses: asyncio.Queue[str] = asyncio.Queue()
    connected = asyncio.Event()

    def on_event(event: str) -> None:
        if event == EVENT_PRESS:
            presses.put_nowait(event)
        elif client.connected:
            connected.set()

    client.add_listener(on_event)
    runner = asyncio.create_task(client.run())
    caption("Connecting...")
    await asyncio.wait_for(connected.wait(), 60)
    await asyncio.sleep(0.5)  # let the connect burst (with the serial) arrive

    if not args.now:
        await client.play_animation([S(1500, WHITE, True), S(1500, OFF, True)], 255)
        caption("Ready: the button is breathing white. Start recording, then press the button.")
        await presses.get()
        await client.play_animation([S(400, OFF)])
        await asyncio.sleep(1.0)

    for text, steps, repeat in SCENES:
        caption(text)
        await client.play_animation(steps, repeat)
        await asyncio.sleep(duration(steps, repeat) + 0.6)

    caption(f"Toggle feedback: two white blinks = your turn; press a few times ({args.toggle_seconds:.0f} s)")
    await client.play_animation([S(150, WHITE), S(150, OFF)], 2)
    await asyncio.sleep(0.8)
    while not presses.empty():
        presses.get_nowait()
    state = False
    count = 0
    deadline = time.monotonic() + args.toggle_seconds
    while (remaining := deadline - time.monotonic()) > 0 and count < 6:
        try:
            await asyncio.wait_for(presses.get(), remaining)
        except TimeoutError:
            break
        state = not state
        count += 1
        color = GREEN if state else RED
        caption(f"  press -> {'ON (green)' if state else 'OFF (red)'}")
        await client.play_animation([S(800, color), *fade_steps(color, OFF, 1200)])
    await asyncio.sleep(2.5)  # let the last feedback finish

    caption("Status light: steady amber, as the LED light entity would show it")
    await client.play_animation([S(1500, AMBER, True), S(3000, AMBER), S(1500, OFF, True)])
    await asyncio.sleep(6.5)
    caption("Done.")
    runner.cancel()
    await link.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
