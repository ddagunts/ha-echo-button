"""Tests for research/ds_tool.py: LEB128, round-trip, and RAM patching. No hardware,
and no dependence on a (private) firmware dump -- uses a synthetic section."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research"))

import ds_tool as DT



def test_leb128_roundtrip():
    for v in (0, 1, 0x7F, 0x80, 0x9C, 0x3FF, 924, 0xFFFF, 0x1FFFFF):
        enc = DT.write_leb(v)
        dec, p = DT.read_leb(enc, 0)
        assert dec == v and p == len(enc)


def _synthetic():
    # 16-byte header + a few records incl. a 0x0103 Data load and a >127-byte record
    # (to exercise multi-byte LEB128), mirroring the real DS structure.
    header = b"BRCMcfgD" + b"\x00\x00\x00\x00" + struct.pack("<I", 0x1234)
    recs = [
        [0x0303, bytearray(b"BCM20735B0 fcbga\x00")],
        [0x0316, bytearray(b"\x00\x00")],
        [0x0103, bytearray(struct.pack("<I", 0x215900) + bytes(range(200)))],  # len 204 -> 2-byte LEB
        [0x0110, bytearray(b"\x01" + b"\x00" * 11)],
    ]
    return DT.Section(header, recs)


def test_section_roundtrip():
    sec = _synthetic()
    blob = sec.to_bytes()
    parsed, end = DT.Section.parse(blob, 0)
    assert end == len(blob)
    assert parsed.to_bytes() == blob
    assert [r[0] for r in parsed.records] == [0x0303, 0x0316, 0x0103, 0x0110]


def test_data_loads_and_patch():
    sec = _synthetic()
    loads = list(sec.data_loads())
    assert loads[0][0] == 0x215900 and len(loads[0][2]) == 200
    # patch 4 bytes at a RAM address inside the Data record
    assert sec.patch_ram(0x215904, b"\xde\xad\xbe\xef")
    _base, _, body = next(sec.data_loads())
    assert body[4:8] == b"\xde\xad\xbe\xef"
    # out-of-range patch is refused
    assert not sec.patch_ram(0x300000, b"\x00")


def test_add_data():
    sec = _synthetic()
    n = len(sec.records)
    sec.add_data(0x280000, b"\x11\x22\x33\x44")
    assert len(sec.records) == n + 1
    blob = sec.to_bytes()
    parsed, _ = DT.Section.parse(blob, 0)
    assert any(a == 0x280000 for a, _, _ in parsed.data_loads())
