#!/usr/bin/env python3
"""Talk to an Amazon Echo Button on its Alexa Gadgets channel (RFCOMM 4).

Framing (from Amazon's Alexa-Gadgets-Raspberry-Pi-Samples, bt_classic/adapter.py):
    F0 | CMD=02 | ERR=00 | SEQ | escaped(payload + checksum_hi + checksum_lo) | F1
    escape: F0/F1/F2 -> F2, (F2 ^ byte);  checksum = (sum(payload) + CMD + ERR) & 0xFFFF
Payload is protobuf agt.Message{bytes payload=1} wrapping a Directive (host->gadget)
or Event (gadget->host): {Header header=1 {namespace=1,name=2,messageId=3,...}, bytes payload=2}.

The button must already be paired (run echobtn_probe.py first).
Run:  python3 research/echobtn_gadget.py --address AA:BB:CC:DD:EE:FF [--channel 4] [--discover] [--duration 600]
"""

import argparse
import datetime
import select
import socket
import time

STX, ETX, ESC = 0xF0, 0xF1, 0xF2
CMD, ERR = 0x02, 0x00


def log(*parts):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


# ---------- minimal protobuf encode/decode ----------

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_field(num: int, value) -> bytes:
    """Length-delimited field (strings, bytes, sub-messages)."""
    if isinstance(value, str):
        value = value.encode()
    return varint(num << 3 | 2) + varint(len(value)) + value


def read_varint(buf: bytes, i: int):
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, i


def pb_decode(buf: bytes, depth=0):
    """Best-effort schemaless decode. Returns list of lines, raises ValueError if not protobuf."""
    lines, i, pad = [], 0, "  " * depth
    while i < len(buf):
        key, i = read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = read_varint(buf, i)
            lines.append(f"{pad}{num}: {v}")
        elif wt == 2:
            n, i = read_varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated bytes")
            sub = buf[i:i + n]
            i += n
            try:
                text = sub.decode()
                if not text.isprintable():
                    text = None
            except UnicodeDecodeError:
                text = None
            inner = None
            if text is None and sub:
                try:
                    inner = pb_decode(sub, depth + 1)
                except ValueError:
                    pass
            if text is not None:
                lines.append(f"{pad}{num}: {text!r}")
            elif inner:
                lines.append(f"{pad}{num}: {{")
                lines += inner
                lines.append(f"{pad}}}")
            else:
                lines.append(f"{pad}{num}: bytes[{len(sub)}] {hexs(sub)}")
        elif wt == 5:
            lines.append(f"{pad}{num}: fixed32 {hexs(buf[i:i + 4])}")
            i += 4
        elif wt == 1:
            lines.append(f"{pad}{num}: fixed64 {hexs(buf[i:i + 8])}")
            i += 8
        else:
            raise ValueError(f"wire type {wt}")
    if i != len(buf):
        raise ValueError("overrun")
    return lines


# ---------- framing ----------

def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (STX, ETX, ESC):
            out += bytes([ESC, ESC ^ b])
        else:
            out.append(b)
    return bytes(out)


class Framer:
    def __init__(self):
        self.seq = 0

    def frame(self, payload: bytes) -> bytes:
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFF
        while self.seq in (STX, ETX, ESC):
            self.seq = (self.seq + 1) & 0xFF
        csum = (sum(payload) + CMD + ERR) & 0xFFFF
        return bytes([STX, CMD, ERR, seq]) + escape(payload + bytes([csum >> 8, csum & 0xFF])) + bytes([ETX])


class Parser:
    """Splits the byte stream into frames; tolerates garbage between frames."""

    def __init__(self):
        self.buf = None
        self.esc = False

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == STX:
                self.buf, self.esc = bytearray(), False
            elif self.buf is None:
                continue
            elif b == ETX:
                frames.append(bytes(self.buf))
                self.buf = None
            elif b == ESC:
                self.esc = True
            else:
                self.buf.append(ESC ^ b if self.esc else b)
                self.esc = False
        return frames


def describe_frame(body: bytes) -> str:
    if len(body) < 5:
        return f"short frame: {hexs(body)}"
    cmd, err, seq, payload, csum = body[0], body[1], body[2], body[3:-2], body[-2] << 8 | body[-1]
    calc = (sum(payload) + cmd + err) & 0xFFFF
    head = (f"cmd={cmd:02x} err={err:02x} seq={seq:02x} len={len(payload)} "
            f"checksum={'ok' if calc == csum else f'BAD (got {csum:04x}, calc {calc:04x})'}")
    try:
        decoded = "\n".join("      " + l for l in pb_decode(payload))
    except ValueError as e:
        decoded = f"      (not protobuf: {e})"
    return f"{head}\n      payload: {hexs(payload)}\n{decoded}"


def discover_directive(message_id="1") -> bytes:
    header = pb_field(1, "Alexa.Discovery") + pb_field(2, "Discover") + pb_field(3, message_id)
    scope = pb_field(1, "BearerToken") + pb_field(2, "")
    directive = pb_field(1, header) + pb_field(2, pb_field(1, scope))
    return pb_field(1, directive)  # agt.Message{payload}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", required=True, help="the button's Bluetooth address (from echobtn_probe.py)")
    ap.add_argument("--channel", type=int, default=4)
    ap.add_argument("--discover", action="store_true", help="send an Alexa.Discovery Discover directive after 5 s")
    ap.add_argument("--duration", type=float, default=600)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    log(f"connecting RFCOMM {args.address} channel {args.channel}")
    sock.connect((args.address, args.channel))
    log("connected; listening (press the button any time)")

    framer, parser = Framer(), Parser()
    start = time.monotonic()
    sent_discover = not args.discover
    while time.monotonic() - start < args.duration:
        if not sent_discover and time.monotonic() - start > 5:
            raw = framer.frame(discover_directive())
            log(f"TX Discover directive ({len(raw)} bytes): {hexs(raw)}")
            sock.sendall(raw)
            sent_discover = True
        r, _, _ = select.select([sock], [], [], 0.5)
        if not r:
            continue
        data = sock.recv(4096)
        if not data:
            log("connection closed by button")
            break
        log(f"RX {len(data)} bytes: {hexs(data)}")
        for body in parser.feed(data):
            log(f"  frame {describe_frame(body)}")
    sock.close()
    log("done")


if __name__ == "__main__":
    main()
