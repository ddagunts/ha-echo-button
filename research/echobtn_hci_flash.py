#!/usr/bin/env python3
"""Write the CYW20735 serial flash over the HCI UART download mode.

Companion to echobtn_hci_dump.py. It reuses the same recovery/download-mode
entry (manual battery power-cycle while the tool holds CTS low) and adds the
write path: load a RAM-resident minidriver, erase, WRITE_RAM an image to the
serial flash, and verify by reading it back.

IMPORTANT, read before flashing a real image:
  * Keep a verified golden dump (dump/sflash.bin) first -- it is the only undo.
  * The preferred write path needs NO minidriver: --erase sectors rewrites only
    the listed 4 KB sectors of a full image with the reverse-engineered erase stub
    plus the ROM's WRITE_RAM, and verifies each (hardware-validated).
  * The minidriver path (--minidriver, chip erase) must use a minidriver MATCHED TO
    THIS SILICON: BCM20735B0 -> minidriver-20735B0-uart.hex from the WICED Studio
    6.0 20735-B0 platform, NOT the 20735B1 one. Untested on this board.
  * --test-write is safe: it writes a pattern to an already-erased, unused flash
    region (default 0xFF070000, which the firmware never reads) with NO erase and
    NO minidriver, then reads it back. Use it to prove the write primitive first.

Modes:
  # safe primitive test (no minidriver, unused region):
  python3 research/echobtn_hci_flash.py --port /dev/ttyUSB0 --test-write

  # rewrite only the sectors a mod changed (fw_mod.py prints them):
  python3 research/echobtn_hci_flash.py --port /dev/ttyUSB0 \
      --flash dump/sflash_white.bin --erase sectors --sectors 0x1e000 --reboot

  # full flash of a built image (needs the B0 minidriver; untested):
  python3 research/echobtn_hci_flash.py --port /dev/ttyUSB0 \
      --minidriver minidriver-20735B0-uart.hex \
      --flash app_download.bin --offset 0x4000 --erase chip
"""

import argparse
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])  # make echobtn_hci_dump importable
import echobtn_hci_dump as D  # noqa: E402

VSC_WRITE_RAM = 0xFC4C
VSC_LAUNCH_RAM = 0xFC4E
VSC_CHIP_ERASE = 0xFFCE
VSC_VERIFY_CRC = 0xFCCC
ERASE_LOWEST_NV = 0xFCBEEEEF  # magic "use lowest valid NV range" for CHIP_ERASE
WRITE_CHUNK = 240            # <= DLMaxWriteSize (249); conservative
SECTOR = 0x1000              # serial-flash erase unit

# ---- Reverse-engineered B0 sector-erase stub (see research/firmware_access.md) ----
# Position-independent Thumb that runs in bare download mode (no DS patch needed):
#   WRSR(0) clear block-protect bits (ROM 0x7e34a) -> WREN (0x7e316) -> send
#   {0x20, addr[23:16], addr[15:8], addr[7:0]} via the write primitive (0x63734,
#   mode=1/SPIFFY2) -> poll RDSR (0x7e2fa) until WIP clear -> WRDI (0x7e484).
# The flash ships with block-protect set (status 0x0c), so the WRSR(0) is REQUIRED --
# without it the erase is silently rejected. HARDWARE-VERIFIED on the 0xFF070000 sector.
# The 24-bit flash sector address is a literal at offset 0x54. ROM addresses are B0-specific.
ERASE_STUB = bytes.fromhex(
    "70b50020114eb047114eb04784b001200090019020208df808000e4c200c8df80"
    "900200a8df80a008df80b400423042202a90120084eb04704b0084eb0474008fb"
    "d2074eb047002070bd00004be3070017e30700ccbbaa0035370600fbe2070085e40700")
ERASE_STUB_ADDR_OFF = 0x54
ERASE_STUB_RAM = 0x00220000   # scratch RAM (the download minidriver's load area)


def make_erase_stub(sector_addr: int) -> bytes:
    """Patch the 24-bit flash sector address into the stub template."""
    s = bytearray(ERASE_STUB)
    s[ERASE_STUB_ADDR_OFF:ERASE_STUB_ADDR_OFF + 4] = struct.pack("<I", sector_addr & 0xFFFFFF)
    return bytes(s)


def parse_intel_hex(path: str):
    """Return (segments, launch_addr).

    segments: list of (abs_addr, bytes) coalesced into contiguous runs.
    launch_addr: from a type-05 Start Linear Address record, or None.
    Handles record types 00 (data), 01 (EOF), 02 (ext segment, <<4),
    04 (ext linear, <<16), 05 (start linear address).
    """
    base = 0
    launch = None
    chunks = []  # (addr, bytes)
    with open(path) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw[0] != ":":
                continue
            b = bytes.fromhex(raw[1:])
            count, addr_hi, addr_lo, rectype = b[0], b[1], b[2], b[3]
            addr = (addr_hi << 8) | addr_lo
            data = b[4:4 + count]
            if (sum(b) & 0xFF) != 0:
                raise ValueError(f"bad checksum in record: {raw}")
            if rectype == 0x00:
                chunks.append((base + addr, data))
            elif rectype == 0x01:
                break
            elif rectype == 0x02:
                base = (data[0] << 8 | data[1]) << 4
            elif rectype == 0x04:
                base = (data[0] << 8 | data[1]) << 16
            elif rectype == 0x05:
                launch = int.from_bytes(data, "big")
            else:
                raise ValueError(f"unhandled record type 0x{rectype:02x}")
    # coalesce contiguous
    chunks.sort()
    segments = []
    for addr, data in chunks:
        if segments and addr == segments[-1][0] + len(segments[-1][1]):
            segments[-1] = (segments[-1][0], segments[-1][1] + data)
        else:
            segments.append((addr, bytearray(data)))
    return [(a, bytes(d)) for a, d in segments], launch


class Flasher(D.Downloader):
    def write_mem(self, addr: int, data: bytes, label: str = "") -> None:
        t0 = time.monotonic()
        for i in range(0, len(data), WRITE_CHUNK):
            chunk = data[i:i + WRITE_CHUNK]
            payload = struct.pack("<I", addr + i) + chunk
            ret = self.command(VSC_WRITE_RAM, payload, timeout=2.0)
            if ret[0] != 0:
                raise D.HciError(f"WRITE_RAM @0x{addr + i:08x} status 0x{ret[0]:02x}")
            if label and (i // WRITE_CHUNK) % 32 == 0:
                pct = 100 * (i + len(chunk)) // len(data)
                rate = (i + len(chunk)) / max(time.monotonic() - t0, 1e-3)
                print(f"\r  {label}: {i + len(chunk):#x}/{len(data):#x} ({pct:3d}%) "
                      f"{rate / 1024:.1f} KiB/s", end="", flush=True)
        if label:
            print()

    def launch_ram(self, addr: int) -> None:
        ret = self.command(VSC_LAUNCH_RAM, struct.pack("<I", addr), timeout=2.0)
        if ret[0] != 0:
            raise D.HciError(f"LAUNCH_RAM @0x{addr:08x} status 0x{ret[0]:02x}")

    def load_minidriver(self, hex_path: str) -> None:
        segments, launch = parse_intel_hex(hex_path)
        if launch is None:
            raise D.HciError("minidriver hex has no launch (type-05) record")
        total = sum(len(d) for _, d in segments)
        D.log(f"Loading minidriver {hex_path}: {total} bytes, launch 0x{launch:08x}")
        for addr, data in segments:
            self.write_mem(addr, data)
        self.launch_ram(launch)
        time.sleep(0.05)
        D.log("Minidriver launched.")

    def chip_erase(self, addr: int = ERASE_LOWEST_NV) -> None:
        D.log(f"CHIP_ERASE (addr 0x{addr:08x})...")
        ret = self.command(VSC_CHIP_ERASE, struct.pack("<I", addr), timeout=10.0)
        if ret[0] != 0:
            raise D.HciError(f"CHIP_ERASE status 0x{ret[0]:02x}")
        D.log("Erase complete.")

    def verify_crc(self, addr: int, length: int) -> int:
        ret = self.command(VSC_VERIFY_CRC, struct.pack("<II", addr, length), timeout=5.0)
        if ret[0] != 0:
            raise D.HciError(f"VERIFY_CRC status 0x{ret[0]:02x}")
        return struct.unpack("<I", ret[1:5])[0]

    def erase_sector(self, sector_off: int, ram: int = ERASE_STUB_RAM) -> None:
        """Erase one 4 KB flash sector (offset into the serial flash) via the RE'd stub."""
        stub = make_erase_stub(sector_off)
        D.log(f"Loading {len(stub)}-byte erase stub to 0x{ram:08x}; erasing flash sector "
              f"0x{sector_off & 0xFFFFFF:06x}...")
        self.write_mem(ram, stub)
        self.launch_ram(ram | 1)  # thumb bit; the ROM LAUNCH uses odd addresses
        time.sleep(0.3)

    def readback_verify(self, addr: int, data: bytes) -> bool:
        got = self.read_mem(addr, len(data), label="verify")
        if got == data:
            D.log(f"Readback OK: {len(data)} bytes @0x{addr:08x} match.")
            return True
        first = next(i for i in range(len(data)) if got[i] != data[i])
        D.log(f"Readback MISMATCH first @0x{addr + first:08x}: wrote {data[first]:02x} got {got[first]:02x}")
        return False

    def flash_sectors(self, image: bytes, sectors: list[int]) -> bool:
        """Rewrite whole 4 KB sectors of a full flash image: erase (RE'd stub), write, verify.

        Needs no minidriver -- this is the hardware-validated bare-ROM path. Sectors that
        already hold the image's bytes are skipped. Stops at the first failure.
        """
        for off in sectors:
            if off % SECTOR or off + SECTOR > len(image):
                raise ValueError(f"0x{off:x} is not a 4 KB sector inside the image")
            want = image[off:off + SECTOR]
            win = D.SFLASH_BASE + off
            if self.read_mem(win, SECTOR, label=f"read 0x{off:05x}") == want:
                D.log(f"Sector 0x{off:05x} already matches; skipping.")
                continue
            self.erase_sector(off)
            if any(b != 0xFF for b in self.read_mem(win, SECTOR, label="erase check")):
                D.log(f"Sector 0x{off:05x} did not erase; stopping (flash unchanged beyond this sector).")
                return False
            self.write_mem(win, want, label=f"write 0x{off:05x}")
            if not self.readback_verify(win, want):
                D.log(f"Sector 0x{off:05x} verify FAILED; rewrite it from the golden image.")
                return False
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cts", choices=("low", "high"), default="low")
    ap.add_argument("--test-write", action="store_true",
                    help="SAFE: write a pattern to an unused erased region and read it back (no erase/minidriver)")
    ap.add_argument("--test-addr", default="0xFF070000")
    ap.add_argument("--erase-sector", metavar="ADDR",
                    help="RE'd stub: erase one 4KB flash sector and verify (e.g. 0xFF070000 or 0x70000). "
                         "HARDWARE-UNTESTED; use the unused 0xFF070000 sector first.")
    ap.add_argument("--minidriver", metavar="HEX",
                    help="B0 minidriver hex (required for --flash unless --erase sectors)")
    ap.add_argument("--flash", metavar="BIN", help="raw image to program")
    ap.add_argument("--offset", default="0x0", help="serial-flash offset for --flash (added to 0xFF000000)")
    ap.add_argument("--erase", choices=("none", "chip", "sectors"), default="none",
                    help="'sectors': BIN is a FULL flash image; erase+write+verify only --sectors "
                         "(no minidriver; the hardware-validated path)")
    ap.add_argument("--sectors", metavar="OFF[,OFF]",
                    type=lambda s: [int(x, 0) for x in s.split(",")],
                    help="flash offsets of the 4 KB sectors to rewrite with --erase sectors "
                         "(fw_mod.py prints them)")
    ap.add_argument("--reboot", action="store_true", help="LAUNCH_RAM 0 to reboot into the new image when done")
    args = ap.parse_args()

    ser = D.open_port(args.port, args.baud, args.cts == "low")
    fl = Flasher(ser, args.port, args.baud, args.cts == "low")
    D.log(f"Opened {args.port}; RTS={'asserted' if ser.rts else 'deasserted'}.")
    try:
        fl.wait_for_download_mode()
    except KeyboardInterrupt:
        D.log("Aborted.")
        return

    if args.test_write:
        addr = int(args.test_addr, 0)
        pattern = bytes((0xA5, 0x5A, 0xC3, 0x3C)) * 8 + b"echobtn-flash-test"
        D.log(f"SAFE test: writing {len(pattern)} bytes to 0x{addr:08x} (unused, expect it was 0xff)...")
        before = fl.read_mem(addr, len(pattern))
        D.log(f"  before: {D.hexs(before)}")
        if any(x != 0xFF for x in before):
            D.log("  WARNING: region is NOT erased (not all 0xff); without erase the write will AND bits. Aborting.")
            ser.close()
            return
        fl.write_mem(addr, pattern)
        ok = fl.readback_verify(addr, pattern)
        D.log("SAFE test write " + ("PASSED - flash programming works in download mode." if ok else "FAILED."))
        ser.close()
        return

    if args.erase_sector:
        addr = int(args.erase_sector, 0)
        off = addr & 0xFFFFFF
        win = D.SFLASH_BASE + off
        before = fl.read_mem(win, 32)
        D.log(f"before erase @0x{win:08x}: {D.hexs(before)}")
        fl.erase_sector(off)
        after = fl.read_mem(win, 32)
        D.log(f"after  erase @0x{win:08x}: {D.hexs(after)}")
        D.log("ERASE OK: sector reads 0xff." if all(b == 0xFF for b in after)
              else "ERASE did NOT clear the sector (stub/ABI needs work).")
        ser.close()
        return

    if args.flash and args.erase == "sectors":
        if not args.sectors:
            sys.exit("--erase sectors needs --sectors (e.g. --sectors 0x1e000)")
        image = open(args.flash, "rb").read()
        ok = fl.flash_sectors(image, args.sectors)
        D.log("All sectors written and verified." if ok else "FAILED; see above.")
        if ok and args.reboot:
            D.log("Rebooting (LAUNCH_RAM 0)...")
            try:
                fl.launch_ram(0)
            except D.HciError as e:
                D.log(f"(reboot command: {e}; power-cycle with serial disconnected instead)")
        ser.close()
        return

    if args.flash:
        if not args.minidriver:
            sys.exit("--flash requires --minidriver (B0!). Refusing to erase/write without it.")
        image = open(args.flash, "rb").read()
        off = int(args.offset, 0)
        addr = D.SFLASH_BASE + off
        fl.load_minidriver(args.minidriver)
        if args.erase == "chip":
            fl.chip_erase()
        D.log(f"Writing {len(image)} bytes to flash offset 0x{off:x} (addr 0x{addr:08x})...")
        fl.write_mem(addr, image, label="flash")
        fl.readback_verify(addr, image)
        if args.reboot:
            D.log("Rebooting (LAUNCH_RAM 0)...")
            try:
                fl.launch_ram(0)
            except D.HciError as e:
                D.log(f"(reboot command: {e}; power-cycle with serial disconnected instead)")
        D.log("Done.")
        ser.close()
        return

    D.log("Nothing to do: pass --test-write or --flash. (Handshake succeeded, so the rig is good.)")
    ser.close()


if __name__ == "__main__":
    main()
