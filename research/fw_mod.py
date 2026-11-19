#!/usr/bin/env python3
"""Build modified Echo Button firmware from the stock serial-flash image.

Mods are small, verified edits to the application's RAM image inside the DS
(BRCMcfgD @ flash 0x4000), made with ds_tool. Each mod checks that the bytes it
changes are exactly the stock ones first, so it refuses to touch an unknown build.

Press flash (the built-in "blue flash on every press")
------------------------------------------------------
The app keeps its default light animations as pre-canned light-command payloads
in initialized data (the same wire format the host's 0x06 command uses):

    01 01 <repeat> <n> + n * (<ms u16 BE> <R> <G> <B> <blend>) + <trigger> <u16 BE>

The handler (0x21d6d8 -> 0x21d5fc) files an animation into a slot by its trigger
byte: 0 = play now, 1 = button-up slot, 2 = button-down slot (0x220ce6), 3 and 4 =
state slots. The button callback (0x21c5fc) plays the button-down slot on every
press. On each connection-state change the app re-loads that slot from one of two
identical defaults, 0x2200a5 and 0x2200b8: 100 ms (0,0,255), then 100 ms off,
trigger 2. Recoloring the first step of both records changes the press flash.

CLI:
  python3 research/fw_mod.py dump/sflash.bin --press-color ffffff -o dump/sflash_white.bin
It prints the 4 KB sectors that differ from the input; flash only those with
  python3 research/echobtn_hci_flash.py --flash dump/sflash_white.bin --erase sectors \\
      --sectors 0x1e000 [--reboot]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ds_tool import Section  # noqa: E402

DS_OFFSET = 0x4000
SECTOR = 0x1000

# The two default button-down animations (see the module docstring).
PRESS_FLASH_RECORDS = (0x2200A5, 0x2200B8)
STOCK_PRESS_FLASH = bytes.fromhex("01010102" "0064" "0000ff" "00" "0064" "000000" "00" "020000")
STEP0_RGB = 6  # offset of step 0's R,G,B within the record


def ram_read(sec: Section, addr: int, n: int) -> bytes:
    for base, _, body in sec.data_loads():
        if base <= addr and addr + n <= base + len(body):
            return bytes(body[addr - base:addr - base + n])
    raise KeyError(f"0x{addr:x}+{n} is not inside a single DS Data record")


def set_press_color(sec: Section, rgb: tuple[int, int, int]) -> None:
    """Recolor the first step of both default press animations (stock: blue)."""
    for rec in PRESS_FLASH_RECORDS:
        cur = ram_read(sec, rec, len(STOCK_PRESS_FLASH))
        if cur != STOCK_PRESS_FLASH:
            raise ValueError(f"press-flash record at 0x{rec:x} is not the stock one: {cur.hex(' ')}")
        if not sec.patch_ram(rec + STEP0_RGB, bytes(rgb)):
            raise ValueError(f"could not patch 0x{rec + STEP0_RGB:x}")


def rebuild_image(flash: bytes, sec: Section, end: int) -> bytes:
    """Splice the re-emitted DS back into the full flash image (length must not change)."""
    ds = sec.to_bytes()
    if len(ds) != end - DS_OFFSET:
        raise ValueError("modified DS changed length; this splice only supports in-place edits")
    return flash[:DS_OFFSET] + ds + flash[end:]


def changed_sectors(a: bytes, b: bytes) -> list[int]:
    return sorted({i & ~(SECTOR - 1) for i in range(len(a)) if a[i] != b[i]})


def parse_rgb(s: str) -> tuple[int, int, int]:
    s = s.removeprefix("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("color must be 6 hex digits, e.g. ffffff")
    return tuple(bytes.fromhex(s))  # type: ignore[return-value]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="stock serial-flash image (e.g. dump/sflash.bin)")
    ap.add_argument("-o", "--out", required=True, help="where to write the modified image")
    ap.add_argument("--press-color", type=parse_rgb, metavar="RRGGBB",
                    help="color of the built-in press flash (stock 0000ff)")
    args = ap.parse_args()

    flash = open(args.image, "rb").read()
    sec, end = Section.parse(flash, DS_OFFSET)
    if sec.to_bytes() != flash[DS_OFFSET:end]:
        sys.exit("DS does not round-trip; refusing to modify")
    if args.press_color:
        set_press_color(sec, args.press_color)
    new = rebuild_image(flash, sec, end)
    open(args.out, "wb").write(new)
    secs = changed_sectors(flash, new)
    print(f"wrote {args.out}; changed sectors: " + (", ".join(f"0x{s:05x}" for s in secs) or "none"))


if __name__ == "__main__":
    main()
