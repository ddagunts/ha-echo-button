#!/usr/bin/env python3
"""Parse and repack the Echo Button's serial-flash config sections (BRCMcfgS / BRCMcfgD).

The Static Section (SS, flash 0x0) and Data Section (DS, flash 0x4000) are each a
16-byte header followed by a stream of records:

    <id: uint16 LE> <len: LEB128> <data[len]>

Record IDs are defined in the WICED `configdef*.hdf`; the ones that matter for the
application are 0x0103 "Data" (<addr:4 LE><bytes> -> load to RAM), 0x0110 "Patch Entry"
(ROM hook), and 0x0106 "Function Call". There are NO CRC records (0x010E/0x010F) in this
firmware, and parse->emit round-trips the real DS byte-for-byte, so a modified image is
just: parse -> edit records -> to_bytes() -> reflash (see echobtn_hci_flash.py).

CLI:
  python3 research/ds_tool.py --list dump/sflash.bin          # list DS records
  python3 research/ds_tool.py --list dump/sflash.bin --off 0  # the SS instead
"""

import argparse
import struct

# Config record IDs from configdef20735B1.hdf (enough to delimit the stream).
VALID_IDS = (
    set(range(0x100, 0x132)) | {0x1F0, 0x201} | set(range(0x300, 0x32D))
    | {0x111, 0x30B, 0x312, 0x315, 0x319, 0x321, 0x322, 0x325, 0x327, 0x329, 0x32B, 0x32C}
    | set(range(0x500, 0x526)) | set(range(0x600, 0x674))
    | set(range(0x700, 0x70B)) | set(range(0x900, 0x904)) | {0xF0}
)
ID_DATA = 0x0103        # <addr:4 LE> <bytes> : load data/code to RAM
ID_PATCH_ENTRY = 0x0110
ID_FUNCTION_CALL = 0x0106


def read_leb(data: bytes, p: int):
    v = s = 0
    while True:
        b = data[p]
        p += 1
        v |= (b & 0x7F) << s
        if not b & 0x80:
            return v, p
        s += 7


def write_leb(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | 0x80 if v else b)
        if not v:
            return bytes(out)


class Section:
    """A parsed BRCMcfg section: a 16-byte header + a list of [id, bytearray] records."""

    def __init__(self, header: bytes, records: list):
        self.header = bytearray(header)
        self.records = records

    @classmethod
    def parse(cls, data: bytes, off: int = 0x4000, valid=VALID_IDS):
        if data[off:off + 7] != b"BRCMcfg":
            raise ValueError(f"no BRCMcfg signature at 0x{off:x}")
        header = data[off:off + 16]
        p = off + 16
        recs = []
        while p + 2 <= len(data):
            rid = struct.unpack("<H", data[p:p + 2])[0]
            if rid not in valid:
                break
            q = p + 2
            ln, q = read_leb(data, q)
            recs.append([rid, bytearray(data[q:q + ln])])
            p = q + ln
        return cls(header, recs), p

    def to_bytes(self) -> bytes:
        out = bytearray(self.header)
        for rid, data in self.records:
            out += struct.pack("<H", rid) + write_leb(len(data)) + data
        return bytes(out)

    def data_loads(self):
        """Yield (ram_addr, record_index, data_bytes) for every 0x0103 Data record."""
        for i, (rid, d) in enumerate(self.records):
            if rid == ID_DATA and len(d) >= 4:
                yield struct.unpack("<I", d[:4])[0], i, d[4:]

    def patch_ram(self, addr: int, new: bytes) -> bool:
        """Overwrite bytes at a RAM address that falls within a single 0x0103 Data record."""
        for base, i, body in self.data_loads():
            if base <= addr and addr + len(new) <= base + len(body):
                off = 4 + (addr - base)
                self.records[i][1][off:off + len(new)] = new
                return True
        return False

    def add_data(self, addr: int, body: bytes, before_index: int | None = None) -> None:
        """Add a new 0x0103 Data record loading `body` to RAM address `addr`."""
        rec = [ID_DATA, bytearray(struct.pack("<I", addr) + body)]
        if before_index is None:
            self.records.append(rec)
        else:
            self.records.insert(before_index, rec)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", metavar="FLASH", required=True, help="serial-flash image to read")
    ap.add_argument("--off", type=lambda s: int(s, 0), default=0x4000, help="section offset (0x4000 DS, 0x0 SS)")
    args = ap.parse_args()
    data = open(args.list, "rb").read()
    sec, end = Section.parse(data, args.off)
    print(f"{sec.header[:8].decode(errors='replace')} @0x{args.off:x}: {len(sec.records)} records, "
          f"ends 0x{end:x}; round-trips: {sec.to_bytes() == data[args.off:end]}")
    import collections
    hist = collections.Counter(r[0] for r in sec.records)
    for rid, n in hist.most_common():
        total = sum(len(d) for i, d in sec.records if i == rid)
        print(f"  0x{rid:04x}: {n:5d} records, {total} data bytes")


if __name__ == "__main__":
    main()
