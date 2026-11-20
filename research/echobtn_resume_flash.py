#!/usr/bin/env python3
"""Flash whole 4 KB sectors of an image in short power windows, resuming across presses.

For a bricked unit whose button only gives a ~2 s power pulse. Bare ROM, no minidriver:
in the ROM's boot-download handler the FIRST WRITE_RAM to 0xFF...... after power-up runs
nvram_detect() (bt_boot_hci_Write_RAMHelper), so each window starts with a no-op flash
write (FF bytes, which can't clear any bit) to bring the flash up. Then, per sector:

  quick check (first chunk, then the rest only if it matches) -> skip if already right
  erase (the RE'd sector-erase stub via LAUNCH_RAM) -> write -> full read-back verify

Progress (verified sectors) is kept in IMAGE.state; a window that closes mid-sector just
redoes that sector next time. Sectors below 0x1000 (SS: MAC + factory data) are refused,
and the unit's factory serial must match --expect-serial before anything is written.

  python3 echobtn_resume_flash.py ../firmware/prebuilt/abutton-0.3.0.bin \\
      --sectors 0x1000,0x4000-0x1e000 --expect-serial <the unit's 16-char serial>
"""

import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import echobtn_hci_dump as D  # noqa: E402
import echobtn_hci_flash as F  # noqa: E402
import echobtn_window_probe as P  # noqa: E402

SECTOR = 0x1000
CHUNK = 240                       # same write size as the hardware-validated flasher
DETECT_ADDR = 0x7FFFC             # no-op FF write here triggers nvram_detect()
SERIAL_ADDR = 0xC00


class WindowClosed(Exception):
    pass


def parse_sectors(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = (int(x, 0) for x in part.split("-"))
            out += list(range(lo, hi + 1, SECTOR))
        else:
            out.append(int(part, 0))
    return out


class Session:
    def __init__(self, ser):
        self.ser = ser
        self.misses = 0

    def _ok(self, ret, what: str):
        if ret is None:
            self.misses += 1
            if self.misses >= 2:
                raise WindowClosed(what)
            return None
        self.misses = 0
        return ret

    def read(self, off: int, n: int) -> bytes:
        for _ in range(3):
            ret = self._ok(P.read_once(self.ser, D.SFLASH_BASE + off, n, 0.3), f"read 0x{off:x}")
            if ret is None:
                continue
            if ret[0] != 0:
                raise D.HciError(f"read 0x{off:05x}: status 0x{ret[0]:02x}")
            if len(ret[1]) == n:
                return ret[1]
        raise WindowClosed(f"read 0x{off:x}")

    def read_range(self, off: int, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            out += self.read(off + len(out), min(CHUNK, n - len(out)))
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        for _ in range(3):
            ret = self._ok(P.write_once(self.ser, addr, data, 0.5), f"write 0x{addr:x}")
            if ret is None:
                continue
            if ret[0] != 0:
                raise D.HciError(f"WRITE_RAM 0x{addr:08x}: status 0x{ret[0]:02x}")
            return
        raise WindowClosed(f"write 0x{addr:x}")

    def erase(self, off: int) -> None:
        self.write(F.ERASE_STUB_RAM, F.make_erase_stub(off))
        ret = self._ok(P.cmd_once(self.ser, F.VSC_LAUNCH_RAM,
                                  struct.pack("<I", F.ERASE_STUB_RAM | 1), 0.5), "launch erase")
        if ret is not None and ret[0] != 0:
            raise D.HciError(f"LAUNCH_RAM erase stub: status 0x{ret[0]:02x}")
        # The ROM replies before running the stub, and the stub blocks the command loop until
        # the erase is done (typ. ~50 ms, max ~400 ms). Wait, then poll with a tiny read.
        time.sleep(0.06)
        deadline = time.monotonic() + 0.6
        while True:
            ret = P.read_once(self.ser, D.SFLASH_BASE + off, 16, 0.15)
            if ret is not None and ret[0] == 0:
                if ret[1] == b"\xff" * 16:
                    return
            if time.monotonic() > deadline:
                if ret is None:
                    raise WindowClosed(f"erase 0x{off:x}")
                raise D.HciError(f"sector 0x{off:05x} did not erase (reads {D.hexs(ret[1])})")
            time.sleep(0.03)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="full 512 KiB flash image")
    ap.add_argument("--sectors", required=True, type=parse_sectors,
                    help="flash offsets, e.g. 0x4000-0x1e000 or 0x2000,0x4000-0x6000")
    ap.add_argument("--expect-serial", required=True,
                    help="factory serial at flash 0xC00 (16 chars); refuse to write any other unit")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cts", choices=("low", "high"), default="low")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore IMAGE.state and re-check every sector on the chip")
    args = ap.parse_args()

    image = open(args.image, "rb").read()
    for off in args.sectors:
        if off % SECTOR or off + SECTOR > len(image):
            sys.exit(f"0x{off:x} is not a 4 KB sector inside the image")
        if off < 0x1000:
            sys.exit("refusing to touch sector 0 (MAC + factory data)")
    serial_want = args.expect_serial.encode()
    if len(serial_want) != 16:
        sys.exit("--expect-serial must be the 16-character factory serial")

    spath = args.image + ".state"
    done: set[int] = set()
    if os.path.exists(spath) and args.fresh:
        os.remove(spath)
    if os.path.exists(spath):
        st = json.load(open(spath))
        D.log(f"Resuming from {spath} (use --fresh after flashing anything else to this unit)")
        if st.get("serial") != args.expect_serial:
            sys.exit(f"{spath} is for {st.get('serial')}, not {args.expect_serial}; move it away")
        done = {int(x, 16) for x in st["verified"]}

    def save():
        tmp = spath + ".tmp"
        json.dump({"serial": args.expect_serial, "verified": sorted(hex(x) for x in done)}, open(tmp, "w"))
        os.replace(tmp, spath)

    todo = lambda: [o for o in args.sectors if o not in done]  # noqa: E731
    ser = D.open_port(args.port, args.baud, args.cts == "low")
    dl = D.Downloader(ser, args.port, args.baud, args.cts == "low")
    windows = 0
    t_start = time.monotonic()
    try:
        while todo():
            D.log(f"{len(args.sectors) - len(todo())}/{len(args.sectors)} sectors verified. "
                  f"Next: 0x{todo()[0]:05x}. Press the button...")
            # A unit that is already up and in the ROM handler (e.g. after an earlier 0xFC2E, or
            # held powered by working firmware) answers READ_RAM but not HCI_Reset: use it as is.
            try:
                ret = P.read_once(dl.ser, 0x0, 4, 0.2)
            except P.SERIAL_ERRORS:
                ret = None
            if ret is not None and ret[0] == 0x0C:
                # ROM mpaf_df_hci_cmd_filter: while the firmware's embedded BT stack is up
                # (btu_hcievt_handler set), vendor commands not in its table get Command
                # Disallowed. AButton always runs the stack; stock only sometimes.
                D.log("The running firmware refuses vendor commands (0x0C: its embedded BT stack "
                      "owns the HCI). Update it over the air instead (firmware/abutton_ble.py ota), "
                      "or, for stock, retry when it is idle. Nothing written.")
                return
            up = ret is not None
            if up:
                D.log("Chip already answering READ_RAM; skipping the HCI_Reset handshake.")
            else:
                dl.wait_for_download_mode()
            windows += 1
            t0 = time.monotonic()
            try:
                dl.ser.set_low_latency_mode(True)
            except (ValueError, *P.SERIAL_ERRORS):
                pass
            s = Session(dl.ser)
            try:
                P.cmd_once(dl.ser, P.VSC_DOWNLOAD_MINIDRIVER, b"", 0.5)
                time.sleep(0.05)
                s.write(D.SFLASH_BASE + DETECT_ADDR, b"\xff\xff\xff\xff")  # -> nvram_detect()
                if s.read(0, 8) != b"BRCMcfgS":
                    raise D.HciError("flash not readable after detect (no BRCMcfgS at 0x0)")
                serial = s.read(SERIAL_ADDR, 16)
                if serial != serial_want:
                    D.log(f"ABORT: unit serial is {serial!r}, expected {serial_want!r}. Nothing written.")
                    return
                for off in todo():
                    want = image[off:off + SECTOR]
                    ts = time.monotonic()
                    if s.read(off, CHUNK) == want[:CHUNK] and s.read_range(off, SECTOR) == want:
                        done.add(off)
                        save()
                        D.log(f"  0x{off:05x} already correct ({time.monotonic() - ts:.2f}s)")
                        continue
                    s.erase(off)
                    if want != b"\xff" * SECTOR:
                        for i in range(0, SECTOR, CHUNK):
                            chunk = want[i:i + CHUNK]
                            if chunk != b"\xff" * len(chunk):
                                s.write(D.SFLASH_BASE + off + i, chunk)
                    got = s.read_range(off, SECTOR)
                    if got != want:
                        first = next(i for i in range(SECTOR) if got[i] != want[i])
                        raise D.HciError(f"sector 0x{off:05x} verify FAILED at +0x{first:x}: "
                                         f"wrote {want[first]:02x} got {got[first]:02x}")
                    done.add(off)
                    save()
                    D.log(f"  0x{off:05x} erased+written+verified ({time.monotonic() - ts:.2f}s)")
            except WindowClosed as e:
                D.log(f"Window {windows} closed after {time.monotonic() - t0:.2f}s ({e}). Release the button.")
            except P.SERIAL_ERRORS as err:
                D.log(f"  serial dropped ({err})")
                try:
                    dl.reopen()
                except D.HciError as e:
                    D.log(f"  {e}")
            except D.HciError as e:
                D.log(f"ERROR: {e}")
                D.log("Stopping; progress saved. Nothing below 0x1000 was touched.")
                return
    except KeyboardInterrupt:
        D.log("Stopped; progress saved, re-run the same command to resume.")
        return
    finally:
        try:
            dl.ser.close()
        except P.SERIAL_ERRORS:
            pass
    D.log(f"All {len(args.sectors)} sectors verified in {windows} windows "
          f"({time.monotonic() - t_start:.0f}s). Next power-up boots the new image.")


if __name__ == "__main__":
    main()
