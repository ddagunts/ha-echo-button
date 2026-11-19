#!/usr/bin/env python3
"""Assemble and personalize AButton flash images (CYW20735B0, 512 KiB serial flash).

Per-unit identity lives in flash sector 0, outside the firmware, so one firmware build
(and one signed OTA image) serves every button and OTA never changes the identity:

  * MAC (BD_ADDR): the static section (BRCMcfgS @0x0) record 0x0300, 6 bytes LE.
  * name: an identity record at 0xD00: b"ABTN", version 1, name length, 26-byte name.
    (Without it the firmware names itself like the stock one, "EchoBtn" + 3 serial chars.)
  * The stock factory data at 0xC00..0xCFF (serial, board revision) is kept.

  # full image from an SDK build, keeping a unit's factory data, MAC and stock name:
  python3 firmware/abutton_image.py assemble build/abutton.hex --base dump/sflash.bin -o unit.bin
  # personalize an image (the "patch" step; works on any assembled image):
  python3 firmware/abutton_image.py patch unit.bin --name "Garage Button" --mac 02:11:22:33:44:55
  python3 firmware/abutton_image.py show unit.bin
Flash: research/echobtn_hci_flash.py --flash unit.bin --erase sectors --sectors <printed list>
"""

import argparse
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research"))
from ds_tool import Section

FLASH_SIZE = 0x80000
FLASH_BASE = 0xFF000000
SECTOR = 0x1000
SS_OFFSET = 0x0000
DS_OFFSET = 0x4000
DS1_BANK_END = 0x42000          # DS1 is 0x4000..0x42000 (DS2, the OTA bank, starts here)
DS_END_RECORD = b"\xfe\x00\x00"   # id 0x00FE, length 0: ends a BRCMcfg section (SDK + stock)
FACTORY = slice(0xC00, 0xD00)           # stock serial + board revision
IDENTITY_ADDR = 0xD00
IDENTITY_MAGIC = b"ABTN"
IDENTITY_VERSION = 1
NAME_MAX = 26
ID_BD_ADDR = 0x0300


# ---- Intel HEX --------------------------------------------------------------------------
def read_hex(path: str | Path) -> dict[int, int]:
    """Return {absolute address: byte} for an Intel HEX file (types 00, 01, 02, 04, 05)."""
    mem: dict[int, int] = {}
    base = 0
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw.startswith(":"):
            continue
        b = bytes.fromhex(raw[1:])
        if sum(b) & 0xFF:
            raise ValueError(f"bad checksum: {raw}")
        n, addr, typ, data = b[0], b[1] << 8 | b[2], b[3], b[4:4 + b[0]]
        if typ == 0x00:
            for i in range(n):
                mem[base + addr + i] = data[i]
        elif typ == 0x01:
            break
        elif typ == 0x02:
            base = (data[0] << 8 | data[1]) << 4
        elif typ == 0x04:
            base = (data[0] << 8 | data[1]) << 16
        elif typ != 0x05:
            raise ValueError(f"unhandled record type {typ:#x}")
    return mem


# ---- identity -----------------------------------------------------------------------------
def parse_mac(s: str) -> bytes:
    """'02:11:22:33:44:55' -> the 6 bytes as stored (little-endian)."""
    if not re.fullmatch(r"[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}", s):
        raise ValueError(f"not a MAC address: {s!r}")
    b = bytes.fromhex(re.sub(r"[:-]", "", s))
    if b in (bytes(6), b"\xff" * 6):
        raise ValueError("all-zero / all-ones MAC is not usable")
    return b[::-1]


def format_mac(stored: bytes) -> str:
    return ":".join(f"{x:02X}" for x in stored[::-1])


def check_name(name: str) -> bytes:
    raw = name.encode("ascii", errors="strict") if name.isascii() else b""
    if not 1 <= len(raw) <= NAME_MAX or any(not 0x20 <= c <= 0x7E for c in raw):
        raise ValueError(f"name must be 1-{NAME_MAX} printable ASCII characters")
    return raw


def _ss(img: bytes) -> tuple[Section, int]:
    sec, end = Section.parse(bytes(img), SS_OFFSET)
    if sec.to_bytes() != bytes(img[SS_OFFSET:end]):
        raise ValueError("static section does not round-trip")
    return sec, end


def get_mac(img: bytes) -> bytes:
    sec, _ = _ss(img)
    for rid, data in sec.records:
        if rid == ID_BD_ADDR and len(data) == 6:
            return bytes(data)
    raise ValueError("no BD_ADDR record in the static section")


def set_mac(img: bytearray, mac: bytes) -> None:
    sec, end = _ss(img)
    for rec in sec.records:
        if rec[0] == ID_BD_ADDR and len(rec[1]) == 6:
            rec[1][:] = mac
            break
    else:
        raise ValueError("no BD_ADDR record in the static section")
    new = sec.to_bytes()
    assert len(new) == end - SS_OFFSET
    img[SS_OFFSET:end] = new


def get_name(img: bytes) -> str | None:
    rec = img[IDENTITY_ADDR:IDENTITY_ADDR + 6 + NAME_MAX]
    if rec[:4] != IDENTITY_MAGIC or rec[4] != IDENTITY_VERSION or not 1 <= rec[5] <= NAME_MAX:
        return None
    return rec[6:6 + rec[5]].decode("ascii", errors="replace")


def set_name(img: bytearray, name: str) -> None:
    raw = check_name(name)
    rec = IDENTITY_MAGIC + bytes([IDENTITY_VERSION, len(raw)]) + raw.ljust(NAME_MAX, b"\x00")
    img[IDENTITY_ADDR:IDENTITY_ADDR + len(rec)] = rec


def factory_serial(img: bytes) -> str | None:
    s = img[0xC00:0xC10]
    return s.decode() if all(0x20 <= c <= 0x7E for c in s) else None


def stock_name(img: bytes) -> str | None:
    s = factory_serial(img)
    return "EchoBtn" + s[13:16] if s else None


# ---- assembly --------------------------------------------------------------------------------
# The SDK .hex writes only the SS (0x0) and DS (0x4000). Everything else -- the VS/NVRAM at
# 0x2000/0x3000 and the OTA second bank from 0x42000 -- is left to the device. With a base
# dump those keep the unit's stock content; without one they stay erased (0xFF). The VS lives
# in a write-protected low region the bare-ROM erase stub can't clear, so blanking it (what a
# 0xFF fill would require) is both wrong and unflashable.
def assemble(hex_mem: dict[int, int], base: bytes | None = None) -> bytearray:
    """Full 512 KiB image from an SDK build; overlaid on `base` (the unit's dump) if given."""
    if base is not None and len(base) != FLASH_SIZE:
        raise ValueError("base image must be a full 512 KiB flash dump")
    img = bytearray(base) if base is not None else bytearray(b"\xff" * FLASH_SIZE)
    for addr, byte in hex_mem.items():
        off = addr - FLASH_BASE if addr >= FLASH_BASE else addr
        if not 0 <= off < FLASH_SIZE:
            raise ValueError(f"hex address {addr:#x} is outside the serial flash")
        img[off] = byte
    _ss(img)
    # Section.parse stops AT the end record (id 0x00FE, length 0: bytes fe 00 00), which
    # it doesn't treat as a record. Keep it: without it the ROM runs off the end of the DS into
    # erased flash and never starts the application; the chip sits in the ROM's boot-download
    # handler for the ~2 s a press powers it. Every image this tool made before 2026-11-19
    # lost it this way. The header's length (bytes 12..15, from 0x10) must cover it.
    _, ds_end = Section.parse(bytes(img), DS_OFFSET)
    if bytes(img[ds_end:ds_end + len(DS_END_RECORD)]) != DS_END_RECORD:
        raise ValueError(f"DS has no end record ({DS_END_RECORD.hex(' ')}) after its last record "
                         f"at 0x{ds_end:x}")
    ds_end += len(DS_END_RECORD)
    ds_len = struct.unpack_from("<I", img, DS_OFFSET + 12)[0]
    if DS_OFFSET + 16 + ds_len != ds_end:
        raise ValueError(f"DS header length 0x{ds_len:x} ends at 0x{DS_OFFSET + 16 + ds_len:x}, "
                         f"but its records + end record end at 0x{ds_end:x}")
    # Blank the rest of the DS1 bank, so after the DS there is only 0xFF (a clean stop for the
    # ROM parser), not leftover stock-DS bytes from the base. Matches the SDK's own hex layout.
    img[ds_end:DS1_BANK_END] = b"\xff" * (DS1_BANK_END - ds_end)
    if base is not None:
        img[FACTORY] = base[FACTORY]
        set_mac(img, get_mac(base))        # the SDK SS carried its own BD_ADDR; restore the unit's
    return img


def changed_sectors(a: bytes, b: bytes) -> list[int]:
    return [s for s in range(0, FLASH_SIZE, SECTOR) if a[s:s + SECTOR] != b[s:s + SECTOR]]


def describe(img: bytes) -> str:
    name = get_name(img)
    return (f"MAC     {format_mac(get_mac(img))}\n"
            f"name    {name if name else '(none: firmware uses ' + repr(stock_name(img) or 'AButton') + ')'}\n"
            f"serial  {factory_serial(img) or '(none)'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assemble", help="build a full flash image from an SDK .hex")
    a.add_argument("hex")
    a.add_argument("--base", type=Path, help="this unit's flash dump (keeps factory data + MAC)")
    a.add_argument("--name")
    a.add_argument("--mac")
    a.add_argument("-o", "--out", type=Path, required=True)
    p = sub.add_parser("patch", help="set the name and/or MAC of an assembled image")
    p.add_argument("image", type=Path)
    p.add_argument("--name")
    p.add_argument("--mac")
    p.add_argument("-o", "--out", type=Path, help="default: modify in place")
    s = sub.add_parser("show")
    s.add_argument("image", type=Path)
    args = ap.parse_args()

    try:
        if args.cmd == "show":
            print(describe(args.image.read_bytes()))
            return
        if args.cmd == "assemble":
            base = args.base.read_bytes() if args.base else None
            img = assemble(read_hex(args.hex), base)
            ref, out = base, args.out
        else:
            img = bytearray(args.image.read_bytes())
            if len(img) != FLASH_SIZE:
                raise ValueError("not a full 512 KiB flash image")
            ref, out = bytes(img), args.out or args.image
        if args.name is not None:
            set_name(img, args.name)
        if args.mac is not None:
            set_mac(img, parse_mac(args.mac))
    except (ValueError, OSError) as e:
        sys.exit(f"error: {e}")
    out.write_bytes(img)
    print(f"wrote {out}\n{describe(img)}")
    if ref is not None:
        secs = changed_sectors(ref, img)
        # The stock firmware write-protects the bottom of the flash (SS + fail-safe + VS,
        # 0x0..0x4000); the bare-ROM erase stub can't clear it. A correct image never needs
        # to change those sectors -- if one shows up, something (a wrong base, a blanked VS)
        # is off, so refuse rather than hand over a list that will fail mid-flash.
        protected = [s for s in secs if s < DS_OFFSET]
        if protected:
            sys.exit("error: image would change write-protected sectors "
                     f"{', '.join(f'0x{s:05x}' for s in protected)}; check --base")
        print("sectors to flash: " + (",".join(f"0x{x:05x}" for x in secs) or "none"))


if __name__ == "__main__":
    main()
