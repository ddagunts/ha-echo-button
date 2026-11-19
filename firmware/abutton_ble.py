#!/usr/bin/env python3
"""Talk to AButton firmware over BLE: adverts, info, LED, settings, name, commands, OTA.

  python3 firmware/abutton_ble.py monitor                    # decode BTHome adverts live
  python3 firmware/abutton_ble.py info     AA:BB:CC:DD:EE:FF # firmware, name, settings, LED
  python3 firmware/abutton_ble.py color    AA:BB:CC:DD:EE:FF ff8800      # steady color; "off"
  python3 firmware/abutton_ble.py animate  AA:BB:CC:DD:EE:FF ff0000:300 000000:300 --repeat 0
  python3 firmware/abutton_ble.py stop     AA:BB:CC:DD:EE:FF            # stop the animation
  python3 firmware/abutton_ble.py config   AA:BB:CC:DD:EE:FF --brightness 128 --press-color 00ff00
  python3 firmware/abutton_ble.py name     AA:BB:CC:DD:EE:FF "Garage Button"   # "" = default
  python3 firmware/abutton_ble.py identify AA:BB:CC:DD:EE:FF            # blink white 3 times
  python3 firmware/abutton_ble.py reboot   AA:BB:CC:DD:EE:FF
  python3 firmware/abutton_ble.py defaults AA:BB:CC:DD:EE:FF            # restore all settings
  python3 firmware/abutton_ble.py ota      AA:BB:CC:DD:EE:FF firmware/prebuilt/abutton-X.ota.signed

Animation steps are COLOR:MS or COLOR:MS:fade (fade from the previous color); --repeat 0
loops until stopped or replaced. Settings (config) are kept on the button; see
`config --help`. The protocol is in custom_components/echo_button/abutton.py.

OTA uses the WICED secure upgrade service (fw_upgrade_lib): enable control-point
notifications, PREPARE (1), DOWNLOAD (2, u32 length), write the signed image to the data
characteristic, VERIFY (3). The button checks SHA-256 + ECDSA against the key built into
it, switches to the new image and reboots. A wrong signature is rejected (status 3) and
the button keeps running its current firmware.
"""

import argparse
import asyncio
import struct
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button"))
import abutton as A  # the shared, Home Assistant-free protocol module

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
OTA_SEC_SERVICE = "c7261110-f425-447a-a1bd-9d7246768bd8"
OTA_CONTROL = "a3dd50bf-f7a7-4e99-838e-570a086c661b"
OTA_DATA = "a2e86c7a-d961-4091-b74f-2409e72efe26"
DIS_CHARS = {"manufacturer": "00002a29-0000-1000-8000-00805f9b34fb",
             "model": "00002a24-0000-1000-8000-00805f9b34fb",
             "firmware": "00002a26-0000-1000-8000-00805f9b34fb"}

CMD_PREPARE, CMD_DOWNLOAD, CMD_VERIFY, CMD_ABORT = 1, 2, 3, 7
STATUS = {0: "ok", 1: "unsupported command", 2: "illegal state", 3: "verification failed",
          4: "invalid image", 5: "invalid image size", 6: "more data", 7: "invalid app id",
          8: "invalid version", 9: "continue"}
BUTTON_EVENTS = {0: "none", 1: "press", 2: "double_press", 3: "triple_press", 4: "long_press",
                 5: "long_double_press", 6: "long_triple_press", 0x80: "hold_press"}


def parse_bthome(data: bytes) -> dict | None:
    """Decode the BTHome v2 objects this firmware sends (packet id, battery, button)."""
    if not data or data[0] & 0xE0 != 0x40 or data[0] & 0x01:
        return None                       # not v2, or encrypted
    out, p = {}, 1
    sizes = {0x00: 1, 0x01: 1, 0x3A: 1}
    while p < len(data):
        oid = data[p]
        if oid not in sizes or p + 1 + sizes[oid] > len(data):
            break
        v = data[p + 1]
        if oid == 0x00:
            out["packet_id"] = v
        elif oid == 0x01:
            out["battery"] = v
        else:
            out["button"] = BUTTON_EVENTS.get(v, f"0x{v:02x}")
        p += 1 + sizes[oid]
    return out


async def monitor(seconds: float | None) -> None:
    last: dict[str, int] = {}

    def seen(dev, adv):
        sd = adv.service_data.get(BTHOME_UUID)
        if sd is None:
            return
        obj = parse_bthome(sd)
        if obj is None or last.get(dev.address) == obj.get("packet_id"):
            return
        last[dev.address] = obj.get("packet_id")
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} {dev.address} {adv.local_name or '?':16s} rssi {adv.rssi:4d}  {obj}", flush=True)

    async with BleakScanner(seen, scanning_mode="active"):
        if seconds:
            await asyncio.sleep(seconds)
        else:
            await asyncio.Event().wait()


async def connect(address: str, tries: int = 3) -> BleakClient:
    """BlueZ often times out on the first connection attempt; retry a few times."""
    last: Exception | None = None
    for attempt in range(tries):
        device = await BleakScanner.find_device_by_address(address, timeout=10)
        if device is None:
            last = RuntimeError(f"{address} not found (is it powered and in range?)")
            continue
        client = BleakClient(device, timeout=20)
        try:
            await client.connect()
            return client
        except (TimeoutError, BleakError) as err:
            last = err
            print(f"  connect attempt {attempt + 1} failed: {err or type(err).__name__}", file=sys.stderr)
    raise RuntimeError(f"could not connect to {address}: {last or 'timeout'}")


def need_abutton(c: BleakClient) -> None:
    if not c.services.get_service(A.SERVICE_UUID):
        raise RuntimeError("no AButton service: firmware older than 0.2.0, or not an AButton")


async def info(address: str) -> None:
    c = await connect(address)
    try:
        for k, uuid in DIS_CHARS.items():
            print(f"{k:13s} {(await c.read_gatt_char(uuid)).decode(errors='replace')}")
        print(f"{'ota service':13s} {'secure' if c.services.get_service(OTA_SEC_SERVICE) else 'MISSING'}")
        if not c.services.get_service(A.SERVICE_UUID):
            print(f"{'abutton':13s} no settings service (firmware older than 0.2.0)")
            return
        print(f"{'name':13s} {(await c.read_gatt_char(A.NAME_UUID)).decode(errors='replace')}")
        led = A.LedState.decode(bytes(await c.read_gatt_char(A.LED_UUID)))
        print(f"{'led':13s} {bytes(led.color).hex()}{' (animating)' if led.animating else ''}")
        print_config(A.Config.decode(bytes(await c.read_gatt_char(A.CONFIG_UUID))))
    finally:
        await c.disconnect()


def print_config(cfg: A.Config) -> None:
    on = lambda bit: "on" if cfg.flag(bit) else "off"
    print(f"{'brightness':13s} {cfg.brightness}/255")
    print(f"{'press color':13s} {bytes(cfg.press_color).hex()}")
    print(f"{'long press':13s} {cfg.long_press_ms} ms")
    print(f"{'multi-click':13s} {cfg.multi_click_ms} ms")
    print(f"{'adverts':13s} every {cfg.advert_interval_ms} ms")
    print(f"{'low battery':13s} below {cfg.low_battery_pct} %, warning {on(A.FLAG_LOW_BATTERY_WARNING)}")
    print(f"{'boot blink':13s} {'battery color' if cfg.flag(A.FLAG_BOOT_BATTERY) else 'white'}")
    print(f"{'restore LED':13s} {on(A.FLAG_RESTORE_LED)}")


async def write(address: str, uuid: str, payloads: list[bytes], what: str) -> None:
    c = await connect(address)
    try:
        need_abutton(c)
        for p in payloads:
            await c.write_gatt_char(uuid, p, response=True)
        print(what)
    finally:
        await c.disconnect()


async def configure(address: str, args: argparse.Namespace) -> None:
    c = await connect(address)
    try:
        need_abutton(c)
        cfg = A.Config.decode(bytes(await c.read_gatt_char(A.CONFIG_UUID)))
        changes = {k: v for k, v in (
            ("brightness", args.brightness), ("long_press_ms", args.long_press_ms),
            ("multi_click_ms", args.multi_click_ms), ("advert_interval_ms", args.advert_interval_ms),
            ("low_battery_pct", args.low_battery_pct),
            ("press_color", A.parse_hex_color(args.press_color) if args.press_color else None),
        ) if v is not None}
        new = A.Config(**{**cfg.__dict__, **changes})
        for bit, value in ((A.FLAG_LOW_BATTERY_WARNING, args.low_battery_warning),
                           (A.FLAG_BOOT_BATTERY, args.boot_battery), (A.FLAG_RESTORE_LED, args.restore_led)):
            if value is not None:
                new = new.with_flag(bit, value == "on")
        if new != cfg:
            await c.write_gatt_char(A.CONFIG_UUID, new.encode(), response=True)
            new = A.Config.decode(bytes(await c.read_gatt_char(A.CONFIG_UUID)))
            print("saved on the button:")
        print_config(new)
    finally:
        await c.disconnect()


def parse_step(text: str) -> A.Step:
    parts = text.split(":")
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "fade"):
        raise argparse.ArgumentTypeError(f"step {text!r}: use COLOR:MS or COLOR:MS:fade")
    try:
        return A.Step(int(parts[1]), A.parse_hex_color(parts[0]), len(parts) == 3)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"step {text!r}: {err}") from err


async def ota(address: str, image: bytes) -> None:
    if image[:8] != b"BRCMcfgD":
        sys.exit("not a WICED DS image (expected the signed .ota.signed file)")
    total = struct.unpack("<I", image[12:16])[0] + 16 + 64
    if total != len(image):
        sys.exit(f"image length {len(image)} != header length + prefix + signature ({total}); not signed?")
    replies: asyncio.Queue[int] = asyncio.Queue()

    async with BleakClient(address, timeout=30) as c:
        if not c.services.get_service(OTA_SEC_SERVICE):
            sys.exit("the device has no secure OTA service (is it running AButton firmware?)")
        await c.start_notify(OTA_CONTROL, lambda _h, d: replies.put_nowait(d[0] if d else 0xFF))

        async def command(payload: bytes, what: str, timeout: float = 30) -> None:
            await c.write_gatt_char(OTA_CONTROL, payload, response=True)
            status = await asyncio.wait_for(replies.get(), timeout)
            if status != 0:
                raise RuntimeError(f"{what}: {STATUS.get(status, status)}")

        await command(bytes([CMD_PREPARE]), "prepare")
        await command(bytes([CMD_DOWNLOAD]) + struct.pack("<I", len(image)), "download", timeout=60)
        chunk = max(20, min(c.mtu_size - 3, 240))
        t0 = time.monotonic()
        for i in range(0, len(image), chunk):
            await c.write_gatt_char(OTA_DATA, image[i:i + chunk], response=True)
            if (i // chunk) % 50 == 0 or i + chunk >= len(image):
                done = min(i + chunk, len(image))
                rate = done / max(time.monotonic() - t0, 1e-3) / 1024
                print(f"\r  {done}/{len(image)} bytes ({100 * done // len(image)}%) {rate:.1f} KiB/s",
                      end="", flush=True)
        print()
        try:
            await command(bytes([CMD_VERIFY]), "verify", timeout=60)
        except RuntimeError:
            await c.write_gatt_char(OTA_CONTROL, bytes([CMD_ABORT]), response=True)
            raise
        print("verified; the button is switching to the new firmware and rebooting")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("monitor", help="decode BTHome adverts")
    m.add_argument("--seconds", type=float)
    for name, help_ in (("info", "firmware, name, settings and LED"), ("stop", "stop the animation"),
                        ("identify", "blink white 3 times"), ("reboot", "restart the button"),
                        ("defaults", "restore default settings, name and LED")):
        sub.add_parser(name, help=help_).add_argument("address")
    col = sub.add_parser("color", help="steady LED color")
    col.add_argument("address")
    col.add_argument("color", help="RRGGBB, or off")
    an = sub.add_parser("animate", help="play an animation")
    an.add_argument("address")
    an.add_argument("steps", nargs="+", type=parse_step, help="COLOR:MS or COLOR:MS:fade")
    an.add_argument("--repeat", type=int, default=1, help="times to play it; 0 = until stopped")
    nm = sub.add_parser("name", help="set the name (empty string = the default name)")
    nm.add_argument("address")
    nm.add_argument("name")
    cf = sub.add_parser("config", help="show or change settings (kept on the button)")
    cf.add_argument("address")
    cf.add_argument("--brightness", type=int, help="1-255, scales every LED color")
    cf.add_argument("--press-color", help="RRGGBB shown while the button is held; 000000 = none")
    cf.add_argument("--long-press-ms", type=int, help="hold time for a long press (300-5000)")
    cf.add_argument("--multi-click-ms", type=int, help="window for double/triple clicks (100-1000)")
    cf.add_argument("--advert-interval-ms", type=int, help="idle BTHome advert interval (100-10000)")
    cf.add_argument("--low-battery-pct", type=int, help="battery level that counts as low (0-100)")
    for flag, help_ in (("--low-battery-warning", "two red blinks after a press when the battery is low"),
                        ("--boot-battery", "boot blink shows the battery (green/amber/red) instead of white"),
                        ("--restore-led", "keep the steady color across reboots")):
        cf.add_argument(flag, choices=("on", "off"), help=help_)
    o = sub.add_parser("ota", help="install a signed firmware image")
    o.add_argument("address")
    o.add_argument("image")
    args = ap.parse_args()

    try:
        if args.cmd == "monitor":
            job = monitor(args.seconds)
        elif args.cmd == "info":
            job = info(args.address)
        elif args.cmd == "color":
            color = None if args.color.lower() == "off" else A.parse_hex_color(args.color)
            job = write(args.address, A.LED_UUID, [A.led_color(color)], f"LED {args.color}")
        elif args.cmd == "animate":
            job = write(args.address, A.LED_UUID, A.led_animation(args.steps, args.repeat),
                        f"playing {len(args.steps)} steps, {'forever' if args.repeat == 0 else f'{args.repeat}x'}")
        elif args.cmd == "stop":
            job = write(args.address, A.LED_UUID, [A.led_stop()], "animation stopped")
        elif args.cmd == "name":
            job = write(args.address, A.NAME_UUID, [A.check_name(args.name)],
                        f"name set to {args.name!r}" if args.name else "name reset to the default")
        elif args.cmd in ("identify", "reboot", "defaults"):
            code = {"identify": A.CMD_IDENTIFY, "reboot": A.CMD_REBOOT, "defaults": A.CMD_DEFAULTS}[args.cmd]
            job = write(args.address, A.COMMAND_UUID, [bytes([code])], f"{args.cmd}: done")
        elif args.cmd == "config":
            job = configure(args.address, args)
        else:
            job = ota(args.address, Path(args.image).read_bytes())
        asyncio.run(job)
    except (TimeoutError, RuntimeError, ValueError, BleakError) as e:
        sys.exit(f"error: {e or type(e).__name__}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
