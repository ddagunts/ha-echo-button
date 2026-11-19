#!/usr/bin/env python3
"""Listen-only (except --reset-first) reader for WICED trace output on the button's HCI UART (TRACE=1 builds).

A TRACE=1 AButton routes WICED_BT_TRACE to the HCI UART as WICED HCI packets:
    0x19 <opcode u16 LE> <len u16 LE> <payload>
This prints each packet's text with a timestamp, plus any other bytes as hex, and marks
silences so a hang (heartbeat stops) can be told from a power loss. It never transmits;
RTS stays asserted so the chip's CTS is low and it may send.

  python3 echobtn_trace.py --port /dev/ttyUSB0
"""

import argparse
import struct
import sys
import time


sys.path.insert(0, __file__.rsplit("/", 1)[0])
import echobtn_hci_dump as D  # noqa: E402
import echobtn_window_probe as P  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl-C)")
    ap.add_argument("--quiet-mark", type=float, default=1.0, help="log a marker after this much silence")
    ap.add_argument("--reset-first", action="store_true",
                    help="send one LAUNCH_RAM 0 (ROM reset vector) to reboot a chip that is up in the "
                         "ROM HCI handler, then listen on the same port so no early trace is lost")
    args = ap.parse_args()

    ser = D.open_port(args.port, args.baud, True)
    dl = D.Downloader(ser, args.port, args.baud, True)
    if args.reset_first:
        import echobtn_hci_flash as F
        D.log(f"LAUNCH_RAM 0 -> {P.cmd_once(dl.ser, F.VSC_LAUNCH_RAM, struct.pack('<I', 0), 0.3)}")
    D.log(f"Listening on {args.port} @ {args.baud} (never transmits after that). Press the button.")
    buf = bytearray()
    last = time.monotonic()
    marked = True
    end = time.monotonic() + args.seconds if args.seconds else None
    try:
        while end is None or time.monotonic() < end:
            try:
                chunk = dl.ser.read(dl.ser.in_waiting or 1)
            except P.SERIAL_ERRORS as err:
                D.log(f"-- serial dropped ({err}); reopening")
                dl.reopen()
                buf.clear()
                continue
            now = time.monotonic()
            if not chunk:
                if not marked and now - last > args.quiet_mark:
                    D.log(f"-- silent for {args.quiet_mark:.1f}s")
                    marked = True
                continue
            last, marked = now, False
            buf += chunk
            while buf:
                if buf[0] == 0x19:
                    if len(buf) < 5:
                        break
                    op, ln = struct.unpack("<HH", buf[1:5])
                    if ln > 1024:                      # not a real WICED HCI header
                        D.log(f"?? {buf[:1].hex()}")
                        del buf[:1]
                        continue
                    if len(buf) < 5 + ln:
                        break
                    payload = bytes(buf[5:5 + ln])
                    del buf[:5 + ln]
                    text = payload.decode("ascii", "replace").rstrip("\r\n\x00")
                    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in payload) >= 0.8 * max(1, len(payload))
                    D.log(f"[{op:04x}] {text}" if printable else f"[{op:04x}] {payload.hex(' ')}")
                elif buf[0] == 0x04 and len(buf) >= 3 and len(buf) >= 3 + buf[2]:
                    ev = bytes(buf[:3 + buf[2]])
                    del buf[:len(ev)]
                    D.log(f"HCI event {ev.hex(' ')}")
                elif buf[0] == 0x04 and len(buf) < 3 + (buf[2] if len(buf) > 2 else 255):
                    break
                else:
                    i = next((k for k in range(1, len(buf)) if buf[k] in (0x19, 0x04)), len(buf))
                    D.log(f"raw {bytes(buf[:i]).hex(' ')}")
                    del buf[:i]
    except KeyboardInterrupt:
        pass
    D.log("Stopped.")


if __name__ == "__main__":
    main()
