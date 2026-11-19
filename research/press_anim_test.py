#!/usr/bin/env python3
"""Test the light command's trigger trailer: set the press animation over the protocol.

Decoded from the firmware (app_map.md, "LED and press feedback"): command 0x06 takes an
optional 3-byte trailer after the steps, <trigger> <delay u16 BE>. Trigger 0 (or no
trailer) plays now; 1 stores the animation as the button-up animation, 2 as the
button-down one (the slot that holds the built-in blue flash). A stored animation does
NOT play when sent; the next press (or release) shows it.

Slot step buffers have no bounds check. Triggers 1 and 2 hold up to 40 steps with the
trailer; triggers 3 and 4 hold only 3 and 2 steps, so this tool never sends those.

Run (Home Assistant disconnected from the button; the button paired with BlueZ):
  python3 research/press_anim_test.py --address AA:BB:CC:DD:EE:FF down-green
then press the button a few times within --listen seconds.

  control      green 1 s, no trailer: must light at once (proves the channel works)
  down-green   trigger 2: press should flash green instead of blue
  up-magenta   trigger 1: release should flash magenta
  down-white   trigger 2: the white press flash (the fw_mod.py result, without reflashing)
  down-dark    trigger 2: no visible press flash
The app re-loads its blue default on connection-state changes, so a reconnect undoes it.
"""

import argparse
import socket
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_pkg = types.ModuleType("echo_button")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button")]
sys.modules["echo_button"] = _pkg
from echo_button.protocol import LightStep, encode_set_light  # noqa: E402
from echobtn_lab import Lab, log  # noqa: E402

OFF = (0, 0, 0)
PRESETS = {
    "control": (None, [LightStep(1000, (0, 255, 0))]),
    "down-green": (2, [LightStep(600, (0, 255, 0)), LightStep(100, OFF)]),
    "up-magenta": (1, [LightStep(600, (255, 0, 255)), LightStep(100, OFF)]),
    "down-white": (2, [LightStep(100, (255, 255, 255)), LightStep(100, OFF)]),
    "down-dark": (2, [LightStep(1, OFF)]),
}


def light_data(trigger: int | None, steps: list[LightStep]) -> bytes:
    """The 0x06 data field, with the trigger trailer when `trigger` is given."""
    data = encode_set_light(steps)
    if trigger is None:
        return data
    if trigger not in (1, 2):
        raise ValueError("only triggers 1 and 2 have room for more than 2 steps")
    if len(data) + 3 > 250:
        raise ValueError("too many steps for the trailer")
    return data + bytes([trigger, 0x00, 0x00])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", required=True, help="the button's Bluetooth address")
    ap.add_argument("--channel", type=int, default=4)
    ap.add_argument("--listen", type=float, default=60, help="seconds to keep logging presses")
    ap.add_argument("preset", choices=PRESETS)
    args = ap.parse_args()

    trigger, steps = PRESETS[args.preset]
    data = light_data(trigger, steps)
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    log(f"connecting {args.address} channel {args.channel}")
    sock.connect((args.address, args.channel))
    lab = Lab(sock)
    threading.Thread(target=lab.reader, daemon=True).start()
    for _ in range(100):  # the button sends its serial right after connecting
        if lab.serial is not None:
            break
        time.sleep(0.1)
    else:
        sys.exit("the button sent no message (and so no serial) within 10 s")
    lab.command(f"# preset {args.preset}: trigger {trigger}")
    lab.command(f"msg 06 {data.hex()}")
    log(f"sent; press the button now (logging for {args.listen:.0f} s)")
    time.sleep(args.listen)


if __name__ == "__main__":
    main()
