#!/usr/bin/env python3
"""Dump CYW20735 memory (ROM, RAM, serial flash) over the Echo Button's HCI UART.

The SoC's HCI UART is on the TMF test pads. It answers the vendor commands READ_RAM /
WRITE_RAM / LAUNCH_RAM in two situations: under the stock firmware's runtime HCI (while
the button is awake; it may refuse with 0x0C while busy, e.g. reconnecting), and in the
ROM's boot-download handler, which runs for the ~2 s a press powers a unit whose firmware
is broken (there, flash reads return 0x12 until the flash is detected; use
echobtn_resume_flash.py / echobtn_resume_read.py for that case). A running AButton
refuses them (0x0C). See research/firmware_access.md.

Wiring (3.3 V adapter, data lines only; the board runs from its own batteries):

    FT232 (3.3V!)        Button pad        signal
    ------------         ----------        ------
    GND            <-->  TMF2              ground
    RXD            <--   TMF9              button HCI TXD  (button -> host)
    TXD   --[1k]--  -->  TMF11             button HCI RXD  (host  -> button)
    RTS   --[1k]--  -->  TMF10             button HCI CTS_N (flow control: held asserted)
    TMF1 (3.3 V): leave unconnected. Powering the board from the adapter browns it out.

Run the tool, then press the button once so the stock firmware is awake (it stays on for
about 10 minutes after a press). Back up the whole serial flash first:

  python3 research/echobtn_hci_dump.py --port /dev/ttyUSB0 --read 0xFF000000 0x80000 dump/sflash.bin

Needs pyserial.
"""

import argparse
import datetime
import glob
import struct
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

# CYW20735 memory map. The serial flash appears at 0xFF000000 (aliased every 512 KiB).
ROM_BASE, ROM_LEN = 0x00000000, 0x00200000          # 2 MiB on-chip ROM
RAM_BASE, RAM_LEN = 0x00200000, 0x00080000          # 512 KiB window (384 KiB real)
SFLASH_BASE = 0xFF000000                             # off-chip serial flash window

HCI_RESET = bytes.fromhex("01030C00")
HCI_RESET_OK = bytes.fromhex("040E0401030C00")
VSC_WRITE_RAM = 0xFC4C
VSC_READ_RAM = 0xFC4D
VSC_LAUNCH_RAM = 0xFC4E
VSC_UPDATE_BAUD = 0xFC18
READ_CHUNK = 251  # max Read_RAM payload


def log(*parts):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


class HciError(Exception):
    pass


def open_port(port: str, baud: int, cts_low: bool) -> serial.Serial:
    ser = serial.Serial(port, baud, timeout=0.1, rtscts=False, dsrdtr=False)
    ser.rts = cts_low  # most USB-TTL adapters drive the pin LOW when RTS is asserted
    ser.dtr = False
    return ser


class Downloader:
    def __init__(self, ser: serial.Serial, port: str, baud: int, cts_low: bool):
        self.ser = ser
        self.port = port
        self.baud = baud
        self.cts_low = cts_low

    def reopen(self) -> None:
        """Reopen the adapter after it drops off the bus (power glitch / re-enumeration)."""
        try:
            self.ser.close()
        except Exception:
            pass
        last = None
        for _ in range(40):  # ~20 s of retries while the adapter comes back
            candidates = [self.port] + sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
            for path in dict.fromkeys(candidates):
                try:
                    self.ser = open_port(path, self.baud, self.cts_low)
                    if path != self.port:
                        log(f"  port re-enumerated: {self.port} -> {path}")
                        self.port = path
                    return
                except (serial.SerialException, OSError) as err:
                    last = err
            time.sleep(0.5)
        raise HciError(f"could not reopen serial port: {last}")

    def resync(self, attempts: int = 25) -> bool:
        """Re-acquire download mode after a stall: pulse HCI_Reset until the chip acks.

        Covers a transient UART glitch, and a chip that reset but came back up (the
        stock firmware's runtime HCI, or the ROM download handler on a unit whose
        firmware doesn't start), so a fresh HCI_Reset handshake re-syncs. Returns True
        once acknowledged.
        """
        for _ in range(attempts):
            try:
                self.ser.reset_input_buffer()
                self.ser.write(HCI_RESET)
                time.sleep(0.05)
                data = self.ser.read(256)
            except (serial.SerialException, OSError):
                try:
                    self.reopen()
                except HciError:
                    return False
                continue
            if HCI_RESET_OK in data:
                try:
                    while self.ser.read(256):  # drain any further queued acks
                        pass
                except (serial.SerialException, OSError):
                    pass
                return True
        return False

    # --- low-level HCI H4 over UART -------------------------------------
    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            if time.monotonic() > deadline:
                raise HciError(f"timeout: wanted {n} bytes, got {len(buf)} ({hexs(buf)})")
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf)

    def read_event(self, timeout: float = 1.0) -> bytes:
        """Read one HCI event packet (0x04 <code> <plen> <params>), return params area."""
        deadline = time.monotonic() + timeout
        # resync on the 0x04 event indicator
        while True:
            b = self._read_exact(1, deadline)
            if b == b"\x04":
                break
        code = self._read_exact(1, deadline)[0]
        plen = self._read_exact(1, deadline)[0]
        params = self._read_exact(plen, deadline)
        return bytes([code]) + params

    def command(self, opcode: int, payload: bytes = b"", timeout: float = 1.0,
                retries: int = 8) -> bytes:
        """Send a VSC and return the Command Complete return-parameters (after the opcode).

        Reply wire format: 04 0E <plen> 01 <op_lo> <op_hi> <status> <data...>.
        Returns <status> + <data...>; raises HciError on a non-zero status.

        On a flaky link the reply (or the command) can be lost to a transient UART
        glitch while the chip stays in download mode. Every VSC used here -- READ_RAM,
        WRITE_RAM, LAUNCH_RAM (erase stub) -- is idempotent, so on a timeout/serial
        drop we flush (reopen if the port vanished) and re-send the same packet rather
        than aborting the whole operation.
        """
        pkt = b"\x01" + struct.pack("<H", opcode) + bytes([len(payload)]) + payload
        for attempt in range(retries + 1):
            try:
                self.ser.reset_input_buffer()
                self.ser.write(pkt)
                deadline = time.monotonic() + timeout
                while True:
                    ev = self.read_event(max(0.05, deadline - time.monotonic()))
                    # ev = <code> <num_cmd_pkts> <op_lo> <op_hi> <return_params...>
                    if ev[0] != 0x0E:
                        continue  # not a Command Complete; skip
                    got_op = struct.unpack("<H", ev[2:4])[0]
                    if got_op == opcode:
                        return ev[4:]  # status + data
                    # stale Command Complete for an earlier command (e.g. a late HCI_Reset
                    # ack 0x0c03 queued during autobaud) -> skip it and keep reading.
            except HciError:
                if attempt >= retries:
                    raise
                time.sleep(0.05)
                self.resync()  # glitch or brownout-reboot: re-acquire download mode
            except (serial.SerialException, OSError):
                if attempt >= retries:
                    raise
                time.sleep(0.05)
                self.reopen()
                self.resync()

    # --- recovery + commands -------------------------------------------
    def wait_for_download_mode(self, poll: float = 0.3) -> None:
        log("Waiting for download mode -- power-cycle the button now (Ctrl-C to abort)...")
        log("  (holding TXD low between probes to avoid back-feeding the board during the pop)")
        buf = bytearray()
        n = 0
        while True:
            try:
                # Hold a BREAK (TXD = 0V) through the idle gap, so a battery pop here
                # can't back-feed the unpowered board and re-enumerate the adapter.
                self.ser.break_condition = True
                time.sleep(poll)
                self.ser.break_condition = False
                self.ser.reset_input_buffer()
                self.ser.write(HCI_RESET)
                time.sleep(0.05)
                data = self.ser.read(256)
            except (serial.SerialException, OSError) as err:
                log(f"  serial dropped ({err}); reopening (power glitch / re-enumeration)...")
                self.reopen()
                buf.clear()
                continue
            if data:
                buf += data
                if HCI_RESET_OK in buf:
                    log(f"Download mode: HCI_Reset acknowledged ({hexs(HCI_RESET_OK)})")
                    # Drain any further queued HCI_Reset acks until the line is quiet,
                    # so the first real command isn't desynced by a stale 0x0c03 reply.
                    try:
                        self.ser.break_condition = False
                        while self.ser.read(256):
                            pass
                    except (serial.SerialException, OSError):
                        pass
                    return
                buf = buf[-32:]  # keep a tail in case the ack straddles two reads
            n += 1
            if n % 10 == 0:
                log(f"  ...still waiting ({n} polls). If it never answers: check power, "
                    f"swap TXD/RXD, or flip --cts.")

    def read_mem(self, addr: int, length: int, label: str = "") -> bytes:
        out = bytearray()
        t0 = time.monotonic()
        while len(out) < length:
            n = min(READ_CHUNK, length - len(out))
            ret = self.command(VSC_READ_RAM, struct.pack("<I", addr + len(out)) + bytes([n]))
            status, data = ret[0], ret[1:]
            if status != 0:
                raise HciError(f"Read_RAM @0x{addr + len(out):08x} status 0x{status:02x}"
                               + (" (Command Disallowed)" if status == 0x12 else ""))
            if len(data) != n:
                raise HciError(f"Read_RAM short: wanted {n}, got {len(data)} @0x{addr + len(out):08x}")
            out += data
            if label and len(out) % (READ_CHUNK * 64) < READ_CHUNK:
                pct = 100 * len(out) // length
                rate = len(out) / max(time.monotonic() - t0, 1e-3)
                print(f"\r  {label}: {len(out):#010x}/{length:#x} ({pct:3d}%) {rate/1024:.1f} KiB/s",
                      end="", flush=True)
        if label:
            print()
        return bytes(out)

    def set_baud(self, baud: int) -> None:
        log(f"Switching baud to {baud}...")
        self.command(VSC_UPDATE_BAUD, b"\x00\x00" + struct.pack("<I", baud), timeout=1.0)
        self.ser.baudrate = baud
        time.sleep(0.05)
        # re-handshake to confirm the new rate is solid
        self.ser.reset_input_buffer()
        self.ser.write(HCI_RESET)
        ev = self.read_event(1.0)
        if bytes([0x0E]) not in ev[:1]:
            raise HciError("no clean reply after baud change")
        log("Baud change confirmed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200, help="initial autobaud rate (default 115200)")
    ap.add_argument("--fast", type=int, metavar="BAUD", default=0,
                    help="escalate to this baud after handshake, e.g. 1000000 or 3000000")
    ap.add_argument("--cts", choices=("low", "high"), default="low",
                    help="level the adapter drives on RTS -> chip CTS_N (low = asserted, lets the chip send; default low)")
    ap.add_argument("--dump-rom", metavar="PATH", help="dump 0x0..0x200000 (2 MiB on-chip ROM)")
    ap.add_argument("--dump-ram", metavar="PATH", help="dump 0x200000..0x280000 (RAM window)")
    ap.add_argument("--read", nargs=3, metavar=("ADDR", "LEN", "PATH"),
                    help="arbitrary read, e.g. --read 0x270000 0x10000 code.bin")
    ap.add_argument("--no-probe", action="store_true", help="skip the 0xFF000000 serial-flash probe")
    args = ap.parse_args()

    # Hold the recovery level on RTS (drives the button's CTS line). On most
    # USB-TTL adapters ser.rts=True asserts the pin LOW.
    ser = open_port(args.port, args.baud, args.cts == "low")
    log(f"Opened {args.port} @ {args.baud}, RTS={'asserted' if ser.rts else 'deasserted'} "
        f"(CTS target {args.cts}).")

    dl = Downloader(ser, args.port, args.baud, args.cts == "low")
    try:
        dl.wait_for_download_mode()
    except KeyboardInterrupt:
        log("Aborted.")
        return

    if args.fast:
        try:
            dl.set_baud(args.fast)
        except HciError as e:
            log(f"Baud escalation failed ({e}); staying at {args.baud}.")
            ser.baudrate = args.baud

    # A short read of ROM proves the command path before any long dump.
    try:
        sample = dl.read_mem(ROM_BASE, 32)
        log(f"ROM @0x0 sample: {hexs(sample)}")
    except HciError as e:
        log(f"Sanity read failed: {e}")

    if not args.no_probe:
        try:
            s = dl.read_mem(SFLASH_BASE, 16)
            log(f"Serial flash @0x{SFLASH_BASE:08x} IS readable here: {hexs(s)}  "
                f"-> Phase 2 can just dump it with --read.")
        except HciError as e:
            log(f"Serial flash probe: {e}")
            log("  (expected 0x12 -- flash dump needs the Phase 2 stub/SWD path.)")

    if args.dump_rom:
        data = dl.read_mem(ROM_BASE, ROM_LEN, label="ROM")
        open(args.dump_rom, "wb").write(data)
        log(f"Wrote {len(data)} bytes -> {args.dump_rom}")
    if args.dump_ram:
        data = dl.read_mem(RAM_BASE, RAM_LEN, label="RAM")
        open(args.dump_ram, "wb").write(data)
        log(f"Wrote {len(data)} bytes -> {args.dump_ram}")
    if args.read:
        addr = int(args.read[0], 0)
        length = int(args.read[1], 0)
        data = dl.read_mem(addr, length, label=f"0x{addr:x}")
        open(args.read[2], "wb").write(data)
        log(f"Wrote {len(data)} bytes @0x{addr:08x} -> {args.read[2]}")

    log("Done. Leave the 3V3 tap connected until finished; re-tap to re-enter if the link drops.")


if __name__ == "__main__":
    main()
