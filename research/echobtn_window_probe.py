#!/usr/bin/env python3
"""Catch the HCI handshake on a bricked unit, try to keep it alive, and log what it does.

A bricked unit answers HCI_Reset only for ~2 s after a power-on, then goes silent; in that
window ROM reads work and serial-flash reads are refused (0x12). The 2 s matches the ROM's
SP805-style watchdog at 0x329000 (interrupt on the first countdown, reset on the second).

  1. wait for the HCI_Reset ack (same loop as echobtn_hci_dump.py; survives re-enumeration)
  2. --wdog: read the watchdog regs, then write 0 to its control reg 0x329008 (what
     wiced_hal_wdog_disable does). If WRITE_RAM is refused there, fall back to a 12-byte
     RAM stub at 0x220000 that does the same store, run with LAUNCH_RAM.
  3. --minidriver HEX: Download_Minidriver (0xFC2E), then load (single-shot writes, progress logged, stop on first failure)
     and launch it
  4. read a few flash words (SS magic, factory serial 0xC00, DS magic)
  5. ping with READ_RAM every --interval s and log alive<->silent transitions

Writes only RAM / the watchdog register, never the serial flash.

  python3 echobtn_window_probe.py --port /dev/ttyUSB0 --wdog
  python3 echobtn_window_probe.py --port /dev/ttyUSB0 --wdog --minidriver minidriver-20735B0-uart.hex
"""

import argparse
import struct
import sys
import termios
import time

import serial

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import echobtn_hci_dump as D  # noqa: E402
import echobtn_hci_flash as F  # noqa: E402

SERIAL_ERRORS = (serial.SerialException, OSError, termios.error)

WDOG_BASE = 0x329000      # ROM watchdog_* use this (literal at ROM 0x18c8)
WDOG_CTRL = WDOG_BASE + 8  # 0 = off; ROM watchdog_HardResetHelper writes 3 (int + reset)
VSC_DOWNLOAD_MINIDRIVER = 0xFC2E  # ROM bthci_cmd_vs_HandleDownload_Minidriver: stop the stack first
STUB_RAM = 0x220000
# ldr r1,=0x329008 ; movs r0,#0 ; str r0,[r1] ; bx lr ; .word 0x329008  (arm-none-eabi-as)
WDOG_OFF_STUB = bytes.fromhex("014900200860704708903200")


def cmd_once(ser: serial.Serial, opcode: int, payload: bytes, timeout: float):
    """Send one HCI command, no retries. Returns (status, data) from its Command Complete,
    or None on silence/garbage (logged)."""
    pkt = b"\x01" + struct.pack("<H", opcode) + bytes([len(payload)]) + payload
    ser.reset_input_buffer()
    ser.write(pkt)
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        buf += ser.read(ser.in_waiting or 1)  # returns as soon as bytes arrive, not after the 0.1 s timeout
        i = buf.find(b"\x04\x0e")
        if i >= 0 and len(buf) - i >= 3 and len(buf) - i >= 3 + buf[i + 2]:
            ev = bytes(buf[i:i + 3 + buf[i + 2]])
            if len(ev) >= 7 and ev[4:6] == struct.pack("<H", opcode):
                return ev[6], ev[7:]
            D.log(f"  (other event: {D.hexs(ev)})")
            buf = buf[i + len(ev):]
    if buf:
        D.log(f"  (unexpected bytes: {D.hexs(bytes(buf[:64]))})")
    return None


def read_once(ser, addr: int, n: int, timeout: float):
    return cmd_once(ser, D.VSC_READ_RAM, struct.pack("<I", addr) + bytes([n]), timeout)


def write_once(ser, addr: int, data: bytes, timeout: float = 0.5):
    return cmd_once(ser, F.VSC_WRITE_RAM, struct.pack("<I", addr) + data, timeout)


def show(label: str, ret) -> None:
    if ret is None:
        D.log(f"{label}: no answer")
    elif ret[0] != 0:
        D.log(f"{label}: status 0x{ret[0]:02x}")
    else:
        D.log(f"{label}: {D.hexs(ret[1])}")


def wdog_regs(ser, label: str):
    ret = read_once(ser, WDOG_BASE, 16, 0.3)
    if ret and ret[0] == 0 and len(ret[1]) == 16:
        load, value, ctrl, _ = struct.unpack("<4I", ret[1])
        D.log(f"{label}: load=0x{load:08x} value=0x{value:08x} ctrl=0x{ctrl:x}")
        return ctrl
    show(label, ret)
    return None


def disable_wdog(ser, t_up: float) -> None:
    wdog_regs(ser, "wdog before")
    time.sleep(0.05)
    wdog_regs(ser, "wdog +50ms  ")  # value should have counted down
    ret = write_once(ser, WDOG_CTRL, b"\x00\x00\x00\x00")
    show("WRITE_RAM wdog ctrl=0", ret)
    if wdog_regs(ser, "wdog after ") == 0:
        D.log(f"  watchdog OFF via WRITE_RAM ({time.monotonic() - t_up:.2f}s after handshake)")
        return
    D.log("  direct write didn't stick; trying the RAM stub...")
    show("WRITE_RAM stub @0x220000", write_once(ser, STUB_RAM, WDOG_OFF_STUB))
    show("LAUNCH_RAM 0x220001", cmd_once(ser, F.VSC_LAUNCH_RAM, struct.pack("<I", STUB_RAM | 1), 0.5))
    time.sleep(0.05)
    ok = wdog_regs(ser, "wdog after stub") == 0
    D.log(f"  watchdog {'OFF via stub' if ok else 'STILL ON'} ({time.monotonic() - t_up:.2f}s after handshake)")


def load_minidriver(ser, path: str, t_up: float) -> bool:
    # Like brcm_patchram / ChipLoad: Download_Minidriver before any RAM write. Without it the
    # ROM's full HCI stack keeps its live data at 0x2207bc+, and the load crashes it at 0x220780.
    show("Download_Minidriver (0xFC2E)", cmd_once(ser, VSC_DOWNLOAD_MINIDRIVER, b"", 1.0))
    time.sleep(0.05)
    segments, launch = F.parse_intel_hex(path)
    total = sum(len(d) for _, d in segments)
    D.log(f"Loading minidriver: {total} bytes in {F.WRITE_CHUNK}-byte writes, launch 0x{launch:08x}")
    done = 0
    for addr, data in segments:
        for i in range(0, len(data), F.WRITE_CHUNK):
            ret = write_once(ser, addr + i, data[i:i + F.WRITE_CHUNK])
            if ret is None or ret[0] != 0:
                show(f"  WRITE_RAM @0x{addr + i:08x} FAILED after {done}/{total} bytes, "
                     f"{time.monotonic() - t_up:.2f}s after handshake", ret)
                return False
            done += len(data[i:i + F.WRITE_CHUNK])
    D.log(f"  {total} bytes written {time.monotonic() - t_up:.2f}s after handshake")
    show("LAUNCH_RAM minidriver", cmd_once(ser, F.VSC_LAUNCH_RAM, struct.pack("<I", launch), 1.0))
    time.sleep(0.1)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cts", choices=("low", "high"), default="low")
    ap.add_argument("--seconds", type=float, default=60.0, help="how long to monitor after the handshake")
    ap.add_argument("--interval", type=float, default=0.1, help="ping period while alive")
    ap.add_argument("--wdog", action="store_true", help="disable the watchdog right after the handshake")
    ap.add_argument("--minidriver", metavar="HEX",
                    help="load + launch this minidriver (RAM only) after the handshake")
    args = ap.parse_args()

    if args.minidriver:
        try:
            F.parse_intel_hex(args.minidriver)  # fail now, not after the handshake
        except (OSError, ValueError) as err:
            sys.exit(f"--minidriver: {err}")

    ser = D.open_port(args.port, args.baud, args.cts == "low")
    dl = D.Downloader(ser, args.port, args.baud, args.cts == "low")
    D.log(f"Opened {args.port}; RTS={'asserted' if ser.rts else 'deasserted'}.")
    try:
        dl.wait_for_download_mode()
    except KeyboardInterrupt:
        D.log("Aborted.")
        return
    t_up = time.monotonic()
    try:
        dl.ser.set_low_latency_mode(True)  # FT232: 16 ms -> 1 ms USB latency per reply
    except (ValueError, *SERIAL_ERRORS) as err:
        D.log(f"  (low-latency mode unavailable: {err})")

    try:
        if args.wdog:
            disable_wdog(dl.ser, t_up)
        if args.minidriver:
            load_minidriver(dl.ser, args.minidriver, t_up)
        for label, addr, n in (("ROM @0x0", 0x0, 8),
                               ("SS magic @0xFF000000", D.SFLASH_BASE, 8),
                               ("serial @0xFF000C00", D.SFLASH_BASE + 0xC00, 16),
                               ("board rev @0xFF000C20", D.SFLASH_BASE + 0xC20, 1),
                               ("DS magic @0xFF004000", D.SFLASH_BASE + 0x4000, 8)):
            ret = read_once(dl.ser, addr, n, 0.5)
            if ret and ret[0] == 0 and label[0] in "SsD":
                D.log(f"{label}: {D.hexs(ret[1])}  {ret[1].decode('ascii', 'replace')!r}")
            else:
                show(label, ret)
    except SERIAL_ERRORS as err:
        D.log(f"  serial dropped during setup ({err})")
        dl.reopen()

    D.log(f"Monitoring for {args.seconds:.0f}s (READ_RAM pings while alive, HCI_Reset probes while silent)...")
    alive, since = True, t_up
    pings = 0
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            try:
                if alive:
                    ok = read_once(dl.ser, 0x0, 4, 0.3) is not None
                    time.sleep(args.interval)
                else:
                    dl.ser.reset_input_buffer()
                    dl.ser.write(D.HCI_RESET)
                    time.sleep(0.1)
                    ok = D.HCI_RESET_OK in dl.ser.read(256)
                    time.sleep(0.2)
            except SERIAL_ERRORS as err:
                D.log(f"  serial dropped ({err}); reopening...")
                try:
                    dl.reopen()
                except D.HciError as e:
                    D.log(f"  {e}")
                    break
                continue
            pings += 1
            if ok != alive:
                now = time.monotonic()
                D.log(f"{'ALIVE again' if ok else 'SILENT'} after {now - since:.2f}s "
                      f"{'silent' if ok else 'alive'} (t+{now - t_up:.2f}s, {pings} pings)")
                alive, since = ok, now
    except KeyboardInterrupt:
        D.log("Stopped.")
    D.log(f"End: {'alive' if alive else 'silent'} for the last {time.monotonic() - since:.2f}s.")
    dl.ser.close()


if __name__ == "__main__":
    main()
