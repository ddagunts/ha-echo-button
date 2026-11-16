#!/usr/bin/env python3
"""Lab tool for the Echo Button gadget channel (RFCOMM 4).

Keeps channel 4 open, logs every frame the button sends (with header bytes),
and sends whatever is written to a control FIFO, one command per line:

  msg <type> <data-hex>   send payload 01 <type> 01 10 <serial> 01 <len> <data>
  payload <hex>           frame and send an arbitrary payload
  raw <hex>               send bytes exactly as given (no framing)
  # text                  just logged, to mark experiments

Run:  python3 research/echobtn_lab.py --address AA:BB:CC:DD:EE:FF --fifo /tmp/lab.fifo > lab.log
Send: echo "msg 06 01010101 07d000ff0000" > /tmp/lab.fifo     (green for 2 s)
Needs a Python with AF_BLUETOOTH sockets (most distro Pythons have it) and a
button already paired with BlueZ.
"""

import argparse
import datetime
import os
import socket
import sys
import threading
import types
from pathlib import Path

_pkg = types.ModuleType("echo_button")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button")]
sys.modules["echo_button"] = _pkg
from echo_button.protocol import ESC, ETX, STX, encode_frame, parse_message  # noqa: E402

def log(*parts):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


class Lab:
    def __init__(self, sock):
        self.sock = sock
        self.seq = 1
        self.serial: bytes | None = None  # learned from the button's first message
        self.lock = threading.Lock()

    def next_seq(self):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFF
        while self.seq in (STX, ETX, ESC):
            self.seq = (self.seq + 1) & 0xFF
        return seq

    def send_payload(self, payload: bytes):
        frame = encode_frame(payload, self.next_seq())
        with self.lock:
            self.sock.sendall(frame)
        log(f"TX payload={hexs(payload)}  frame={hexs(frame)}")

    def reader(self):
        buf, escaped = None, False
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError as err:
                log(f"RX error {err}")
                os._exit(1)
            if not data:
                log("RX connection closed by button")
                os._exit(1)
            for b in data:
                if b == STX:
                    buf, escaped = bytearray(), False
                elif buf is None:
                    log(f"RX stray byte {b:02x}")
                elif b == ETX:
                    self.on_frame(bytes(buf))
                    buf = None
                elif b == ESC:
                    escaped = True
                else:
                    buf.append(ESC ^ b if escaped else b)
                    escaped = False

    def on_frame(self, body: bytes):
        if len(body) < 5:
            log(f"RX short frame {hexs(body)}")
            return
        cmd, err, seq, payload = body[0], body[1], body[2], body[3:-2]
        csum = body[-2] << 8 | body[-1]
        ok = (sum(payload) + cmd + err) & 0xFFFF == csum
        msg = parse_message(payload)
        if msg is not None:
            self.serial = msg.serial.encode()
            what = f"type={msg.type:#04x} data={hexs(msg.data)}"
        else:
            what = f"payload={hexs(payload)}"
        log(f"RX cmd={cmd:02x} err={err:02x} seq={seq:02x} csum={'ok' if ok else 'BAD'} {what}")

    def command(self, line: str):
        line = line.strip()
        if not line:
            return
        if line.startswith("#"):
            log(f"NOTE {line[1:].strip()}")
            return
        verb, _, rest = line.partition(" ")
        try:
            if verb == "msg":
                if self.serial is None:
                    log("!! serial not known yet (the button sends it right after connecting)")
                    return
                mtype, _, data_hex = rest.strip().partition(" ")
                data = bytes.fromhex(data_hex)
                self.send_payload(bytes([0x01, int(mtype, 16), 0x01, len(self.serial)]) + self.serial
                                  + bytes([0x01, len(data)]) + data)
            elif verb == "payload":
                self.send_payload(bytes.fromhex(rest))
            elif verb == "raw":
                raw = bytes.fromhex(rest)
                with self.lock:
                    self.sock.sendall(raw)
                log(f"TX raw={hexs(raw)}")
            else:
                log(f"?? unknown command {line!r}")
        except (ValueError, OSError) as err:
            log(f"!! {line!r}: {err}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", required=True, help="the button's Bluetooth address")
    ap.add_argument("--channel", type=int, default=4)
    ap.add_argument("--fifo", required=True)
    args = ap.parse_args()

    if not os.path.exists(args.fifo):
        os.mkfifo(args.fifo)
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    log(f"connecting {args.address} channel {args.channel}")
    sock.connect((args.address, args.channel))
    log("connected")
    lab = Lab(sock)
    threading.Thread(target=lab.reader, daemon=True).start()
    while True:
        with open(args.fifo) as fifo:  # blocks until a writer opens it
            for line in fifo:
                lab.command(line)


if __name__ == "__main__":
    main()
