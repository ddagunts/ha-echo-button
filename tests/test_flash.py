"""Off-hardware tests for research/echobtn_hci_flash.py: Intel-HEX parsing and
WRITE_RAM framing against a fake loopback serial. No Bluetooth, no device."""

import sys
import types
from pathlib import Path

import pytest

_research = Path(__file__).resolve().parent.parent / "research"
sys.path.insert(0, str(_research))

# research/*.py import pyserial at module load; stub it if it's absent.
try:
    import serial  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["serial"] = types.SimpleNamespace(Serial=object, SerialException=Exception)

import echobtn_hci_flash as F  # noqa: E402


def test_parse_intel_hex_minidriver_header(tmp_path):
    # The real 20719B2 minidriver header from the AIROC doc: ext-segment 0x0022,
    # then two 16-byte data records at 0x0000 and 0x0010 -> abs 0x220000/0x220010,
    # plus a type-05 launch at 0x00220001 and EOF.
    hexfile = tmp_path / "md.hex"
    hexfile.write_text(
        ":020000040022D8\n"
        ":100000002DE9F0412C480024264684602B4800F05E\n"
        ":100010005FF82B4D6E626E6300F03DFE01F05CFBFD\n"
        ":0400000500220001D4\n"
        ":00000001FF\n"
    )
    segments, launch = F.parse_intel_hex(str(hexfile))
    assert launch == 0x00220001
    # the two 16-byte records are contiguous -> coalesced into one 32-byte segment
    assert len(segments) == 1
    addr, data = segments[0]
    assert addr == 0x00220000
    assert len(data) == 32
    assert data[:4] == bytes.fromhex("2DE9F041")


def test_parse_intel_hex_rejects_bad_checksum(tmp_path):
    hexfile = tmp_path / "bad.hex"
    hexfile.write_text(":0200000400220000\n")  # wrong checksum
    with pytest.raises(ValueError):
        F.parse_intel_hex(str(hexfile))


class FakeSerial:
    """Minimal serial stand-in that answers WRITE_RAM with a Command Complete."""

    def __init__(self):
        self.written = bytearray()
        self.rx = bytearray()
        self.rts = self.dtr = False
        self.baudrate = 115200

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, data):
        self.written += data
        # reply: 04 0E <4> 01 <op_lo op_hi> <status=0>
        self.rx += bytes([0x04, 0x0E, 0x04, 0x01]) + data[1:3] + bytes([0x00])

    def read(self, n=1):
        take = self.rx[:n]
        del self.rx[:n]
        return bytes(take)

    def close(self):
        pass


def test_write_mem_frames_write_ram():
    fl = F.Flasher(FakeSerial(), "fake", 115200, True)
    fl.write_mem(0xFF000000, b"\xde\xad\xbe\xef")
    sent = fl.ser.written
    # 01 4C FC <len=4+4=08> <addr LE: 00 00 00 ff> de ad be ef
    assert sent == bytes.fromhex("014CFC080000 00FF DEADBEEF".replace(" ", ""))


def test_write_mem_chunks_large_payload():
    fl = F.Flasher(FakeSerial(), "fake", 115200, True)
    big = bytes(500)
    fl.write_mem(0xFF000000, big)
    # 500 bytes / 240 per chunk -> 3 WRITE_RAM commands
    n_cmds = fl.ser.written.count(b"\x01\x4c\xfc")
    assert n_cmds == 3


def test_make_erase_stub_patches_address():
    s = F.make_erase_stub(0xFF074000)
    # 24-bit address goes little-endian at offset 0x54
    import struct
    assert struct.unpack("<I", s[0x54:0x58])[0] == 0x074000
    assert len(s) == len(F.ERASE_STUB)
    # template is unchanged elsewhere (prologue push {r4,r5,r6,r7,lr})
    assert s[:2] == bytes.fromhex("70b5")


class FakeFlash(F.Flasher):
    """A Flasher over an in-memory 'serial flash' (no serial framing)."""

    def __init__(self, contents: bytes, erase_works: bool = True):
        self.mem = bytearray(contents)
        self.erase_works = erase_works
        self.erased = []

    def _off(self, addr):
        return addr - F.D.SFLASH_BASE

    def read_mem(self, addr, length, label=""):
        o = self._off(addr)
        return bytes(self.mem[o:o + length])

    def erase_sector(self, sector_off, ram=F.ERASE_STUB_RAM):
        self.erased.append(sector_off)
        if self.erase_works:
            self.mem[sector_off:sector_off + F.SECTOR] = b"\xff" * F.SECTOR

    def write_mem(self, addr, data, label=""):
        o = self._off(addr)
        # NOR semantics: programming can only clear bits
        self.mem[o:o + len(data)] = bytes(a & b for a, b in zip(self.mem[o:o + len(data)], data, strict=True))


def test_flash_sectors_rewrites_only_changed_sector():
    old = bytes(range(256)) * 32  # 8 KB = 2 sectors
    new = bytearray(old)
    new[0x1234] ^= 0xFF  # change one byte in the second sector
    fl = FakeFlash(old)
    assert fl.flash_sectors(bytes(new), [0x0000, 0x1000])
    assert fl.mem == new
    assert fl.erased == [0x1000]  # the matching first sector was skipped


def test_flash_sectors_stops_if_erase_fails():
    old = bytes(F.SECTOR)
    new = b"\x01" * F.SECTOR
    fl = FakeFlash(old, erase_works=False)
    assert not fl.flash_sectors(new, [0])
    assert fl.mem == old  # nothing written


def test_flash_sectors_rejects_unaligned():
    fl = FakeFlash(bytes(2 * F.SECTOR))
    with pytest.raises(ValueError):
        fl.flash_sectors(bytes(2 * F.SECTOR), [0x800])
