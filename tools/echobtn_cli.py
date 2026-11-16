#!/usr/bin/env python3
"""Run the integration's Bluetooth code outside Home Assistant, for testing.

  python3 tools/echobtn_cli.py list            # Echo Buttons BlueZ has paired
  python3 tools/echobtn_cli.py pair            # hold the button until orange first
  python3 tools/echobtn_cli.py run AA:BB:...   # print presses
"""

import argparse
import asyncio
import datetime
import logging
import sys
import types
from pathlib import Path

# Import custom_components/echo_button/* without running its Home Assistant __init__.py.
_pkg = types.ModuleType("echo_button")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button")]
sys.modules["echo_button"] = _pkg

from echo_button import bluez  # noqa: E402
from echo_button.client import EVENT_CONNECTION, EchoButtonClient  # noqa: E402
from echo_button.link import BluezLink  # noqa: E402


async def run(address: str) -> None:
    link = BluezLink()
    await link.start()
    client = EchoButtonClient(address, link)

    def on_event(event: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if event == EVENT_CONNECTION:
            print(ts, "CONNECTED" if client.connected else "DISCONNECTED", client.serial or "", flush=True)
        else:
            print(ts, "EVENT", event, flush=True)

    client.add_listener(on_event)
    await client.run()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("pair")
    run_p = sub.add_parser("run")
    run_p.add_argument("address")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("dbus_fast").setLevel(logging.INFO)

    if args.cmd == "list":
        for address, name in await bluez.paired_buttons():
            print(address, name)
    elif args.cmd == "pair":
        print("paired:", *await bluez.pair_new_button())
    else:
        await run(args.address)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
