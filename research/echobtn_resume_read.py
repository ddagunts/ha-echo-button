#!/usr/bin/env python3
"""Read a memory range over HCI in short power windows, resuming across presses.

A bricked unit is only powered while its button is held, so a long read has to be done
in pieces. This tool reads READ_RAM chunks into PATH and records progress in PATH.map
(one byte per chunk: 0 = missing, 1 = read, 2 = refused by the chip). When the chip goes
silent it goes back to waiting for the next handshake and resumes at the first missing
chunk. Re-running with the same PATH resumes too. Read-only.

  python3 echobtn_resume_read.py 0x200000 0x60000 ../dump/ram_bricked.bin
  python3 echobtn_resume_read.py 0x200000 0x60000 ../dump/ram_bricked.bin --first 0x21f000 0x3000
  python3 echobtn_resume_read.py 0xFF000000 0x80000 ../dump/sflash_47u_now.bin \
      --minidriver ../minidriver-20735B0-uart.hex      # serial flash: needs the minidriver

--minidriver re-loads it (Download_Minidriver + RAM load + launch, ~1.2 s) at the start of
every window, since each power-off clears RAM. Still writes nothing to the serial flash.

Press and hold the button; release when it says the window closed (or keep it held).
"""

import argparse
import os
import sys
import time


sys.path.insert(0, __file__.rsplit("/", 1)[0])
import echobtn_hci_dump as D  # noqa: E402
import echobtn_window_probe as P  # noqa: E402

CHUNK = 240


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("addr", type=lambda s: int(s, 0))
    ap.add_argument("length", type=lambda s: int(s, 0))
    ap.add_argument("path")
    ap.add_argument("--first", nargs=2, metavar=("ADDR", "LEN"), type=lambda s: int(s, 0),
                    help="read this sub-range before the rest")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cts", choices=("low", "high"), default="low")
    ap.add_argument("--minidriver", metavar="HEX", help="load + launch this minidriver every window")
    args = ap.parse_args()
    if args.minidriver:
        try:
            P.F.parse_intel_hex(args.minidriver)
        except (OSError, ValueError) as err:
            sys.exit(f"--minidriver: {err}")

    n = (args.length + CHUNK - 1) // CHUNK
    mpath = args.path + ".map"
    if os.path.exists(args.path) and os.path.exists(mpath):
        state = bytearray(open(mpath, "rb").read())
        if len(state) != n or os.path.getsize(args.path) != args.length:
            sys.exit(f"{args.path}/.map exist but don't match this range; move them away first")
    else:
        state = bytearray(n)
        with open(args.path, "wb") as f:
            f.truncate(args.length)
        open(mpath, "wb").write(state)
    out = open(args.path, "r+b")

    order = list(range(n))
    if args.first:
        lo = max(0, (args.first[0] - args.addr) // CHUNK)
        hi = min(n, (args.first[0] + args.first[1] - args.addr + CHUNK - 1) // CHUNK)
        order = list(range(lo, hi)) + [i for i in order if not lo <= i < hi]

    def save_map():
        tmp = mpath + ".tmp"
        open(tmp, "wb").write(state)
        os.replace(tmp, mpath)

    def remaining():
        return sum(1 for s in state if s == 0)

    ser = D.open_port(args.port, args.baud, args.cts == "low")
    dl = D.Downloader(ser, args.port, args.baud, args.cts == "low")
    windows = 0
    try:
        while remaining():
            D.log(f"{n - remaining()}/{n} chunks done ({remaining() * CHUNK / 1024:.0f} KiB left). "
                  f"Press and hold the button...")
            dl.wait_for_download_mode()
            windows += 1
            try:
                dl.ser.set_low_latency_mode(True)
            except (ValueError, *P.SERIAL_ERRORS):
                pass
            if args.minidriver:
                try:
                    if not P.load_minidriver(dl.ser, args.minidriver, time.monotonic()):
                        D.log("Window closed before the minidriver was up. Release the button.")
                        continue
                except P.SERIAL_ERRORS as err:
                    D.log(f"  serial dropped during minidriver load ({err})")
                    try:
                        dl.reopen()
                    except D.HciError as e:
                        D.log(f"  {e}")
                    continue
            t0, got, misses = time.monotonic(), 0, 0
            for i in order:
                if state[i]:
                    continue
                a = args.addr + i * CHUNK
                ln = min(CHUNK, args.length - i * CHUNK)
                try:
                    ret = P.read_once(dl.ser, a, ln, 0.3)
                except P.SERIAL_ERRORS as err:
                    D.log(f"  serial dropped ({err})")
                    try:
                        dl.reopen()
                    except D.HciError as e:
                        D.log(f"  {e}")
                    break
                if ret is None:
                    misses += 1
                    if misses >= 2:
                        break
                    continue
                misses = 0
                status, data = ret
                if status == 0 and len(data) == ln:
                    out.seek(i * CHUNK)
                    out.write(data)
                    state[i] = 1
                    got += 1
                elif status != 0:
                    D.log(f"  0x{a:08x}: refused, status 0x{status:02x}; leaving zeros")
                    state[i] = 2
                else:
                    continue  # short reply: retry this chunk next window
                if got % 16 == 0:
                    out.flush()
                    save_map()
            out.flush()
            save_map()
            D.log(f"Window {windows} closed: {got} chunks ({got * CHUNK / 1024:.1f} KiB) "
                  f"in {time.monotonic() - t0:.1f}s. Release the button.")
    except KeyboardInterrupt:
        D.log("Stopped; progress saved, re-run the same command to resume.")
    finally:
        out.flush()
        out.close()
        save_map()
        try:
            dl.ser.close()
        except P.SERIAL_ERRORS:
            pass
    if not remaining():
        refused = sum(1 for s in state if s == 2)
        D.log(f"Complete: {args.path} ({args.length} bytes, {windows} windows"
              + (f", {refused} chunks refused" if refused else "") + ").")


if __name__ == "__main__":
    main()
