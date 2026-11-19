"""Tests for the AButton firmware tools (firmware/*.py). Synthetic images only: no SDK,
no hardware, no private flash dump."""

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "firmware"))
sys.path.insert(0, str(ROOT / "research"))

import abutton_image as AI  # noqa: E402
import ota_sign as OS  # noqa: E402
from ds_tool import Section  # noqa: E402


# ---- helpers ------------------------------------------------------------------------------
def _ss(bd_addr: bytes) -> bytes:
    header = b"BRCMcfgS" + bytes(4) + struct.pack("<I", 0x11D)
    return Section(header, [
        [0x0101, bytearray(b"\xc0\x5d\x00\x00")],                 # 24 MHz crystal
        [0x0300, bytearray(bd_addr)],
        [0x0100, bytearray(b"\x00\x40" + bytes(26))],             # config layout
    ]).to_bytes()


def _ds(end_record: bool = True) -> bytes:
    """A DS as the SDK writes it: header (length counts from 0x10), records, end record."""
    recs = Section(bytes(16), [[0x0103, bytearray(struct.pack("<I", 0x2183A4) + bytes(range(64)))]]).to_bytes()[16:]
    tail = AI.DS_END_RECORD if end_record else b""
    return b"BRCMcfgD" + bytes(4) + struct.pack("<I", len(recs) + len(tail)) + recs + tail


def _write_hex(path: Path, chunks: dict[int, bytes]) -> None:
    lines = []
    for addr, data in chunks.items():
        lines.append(_rec(0x04, 0, struct.pack(">H", addr >> 16)))
        for i in range(0, len(data), 16):
            lines.append(_rec(0x00, (addr + i) & 0xFFFF, data[i:i + 16]))
    lines.append(":00000001FF")
    path.write_text("\n".join(lines) + "\n")


def _rec(typ: int, addr: int, data: bytes) -> str:
    b = bytes([len(data), addr >> 8, addr & 0xFF, typ]) + data
    return ":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper()


def _sdk_hex(tmp_path: Path) -> Path:
    h = tmp_path / "abutton.hex"
    _write_hex(h, {0xFF000000: _ss(bytes.fromhex("3169075b7320")), 0xFF004000: _ds()})
    return h


def _stock_base() -> bytes:
    img = bytearray(b"\xff" * AI.FLASH_SIZE)
    ss = _ss(bytes.fromhex("554433221100"))                      # 00:11:22:33:44:55
    img[:len(ss)] = ss
    img[0xC00:0xC10] = b"G090XX0000000ABC"
    img[0xC20] = 1
    return bytes(img)


# ---- abutton_image ------------------------------------------------------------------------
def test_mac_parse_and_format():
    stored = AI.parse_mac("00:11:22:33:44:55")
    assert stored == bytes.fromhex("554433221100")
    assert AI.format_mac(stored) == "00:11:22:33:44:55"
    assert AI.parse_mac("001122-334455") == stored
    for bad in ("00:11:22:33:44", "zz:11:22:33:44:55", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
        with pytest.raises(ValueError):
            AI.parse_mac(bad)


def test_assemble_keeps_factory_data_and_mac(tmp_path):
    base = _stock_base()
    img = AI.assemble(AI.read_hex(_sdk_hex(tmp_path)), base)
    assert len(img) == AI.FLASH_SIZE
    assert AI.format_mac(AI.get_mac(img)) == "00:11:22:33:44:55"     # the unit's MAC, not the SDK's
    assert img[0xC00:0xD00] == base[0xC00:0xD00]
    assert AI.get_name(img) is None and AI.stock_name(img) == "EchoBtnABC"
    assert bytes(img[AI.DS_OFFSET:AI.DS_OFFSET + len(_ds())]) == _ds()


def test_assemble_keeps_the_ds_end_record(tmp_path):
    # Regression: assemble() blanked from where Section.parse stopped, i.e. AT the fe 00 00
    # end record, so the ROM ran off the DS and never started the app (both units, 2026-11).
    img = AI.assemble(AI.read_hex(_sdk_hex(tmp_path)), _stock_base())
    end = AI.DS_OFFSET + len(_ds())
    assert bytes(img[end - 3:end]) == AI.DS_END_RECORD
    assert set(img[end:AI.DS1_BANK_END]) == {0xFF}


def test_assemble_refuses_a_ds_without_end_record(tmp_path):
    h = tmp_path / "noend.hex"
    _write_hex(h, {0xFF000000: _ss(bytes.fromhex("3169075b7320")), 0xFF004000: _ds(end_record=False)})
    with pytest.raises(ValueError, match="end record"):
        AI.assemble(AI.read_hex(h), _stock_base())


def test_patch_name_and_mac_touch_only_sector_0(tmp_path):
    img = AI.assemble(AI.read_hex(_sdk_hex(tmp_path)), _stock_base())
    before = bytes(img)
    AI.set_name(img, "Garage Button")
    AI.set_mac(img, AI.parse_mac("02:11:22:33:44:55"))
    assert AI.get_name(img) == "Garage Button"
    assert AI.format_mac(AI.get_mac(img)) == "02:11:22:33:44:55"
    assert AI.changed_sectors(before, img) == [0]
    rec = img[0xD00:0xD20]
    assert rec[:6] == b"ABTN\x01\x0d" and rec[6:19] == b"Garage Button" and rec[19:] == bytes(13)
    # the static section still parses and keeps its other records
    ss, _ = Section.parse(bytes(img), 0)
    assert [r[0] for r in ss.records] == [0x0101, 0x0300, 0x0100]


def test_name_validation():
    img = bytearray(_stock_base())
    for bad in ("", "x" * 27, "café", "tab\there"):
        with pytest.raises(ValueError):
            AI.set_name(img, bad)
    AI.set_name(img, "x" * 26)
    assert AI.get_name(img) == "x" * 26


def test_hex_checksum_is_checked(tmp_path):
    h = tmp_path / "bad.hex"
    h.write_text(":0400000001020304F0\n:00000001FF\n")
    with pytest.raises(ValueError):
        AI.read_hex(h)


# ---- ota_sign -----------------------------------------------------------------------------
def test_keygen_sign_verify(tmp_path):
    key_path = tmp_path / "k.pem"
    key = OS.keygen(key_path)
    assert key_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        OS.keygen(key_path)                       # never silently replace the signing key
    image = b"BRCMcfgD" + bytes(4) + struct.pack("<I", 100) + bytes(range(100))
    signed = OS.sign(image, key)
    assert signed[:-64] == image and len(signed) == len(image) + 64
    assert OS.verify(signed, key.public_key())
    tampered = bytearray(signed)
    tampered[20] ^= 1
    assert not OS.verify(bytes(tampered), key.public_key())
    other = OS.keygen(tmp_path / "other.pem")
    assert not OS.verify(signed, other.public_key())


def test_public_key_c_words(tmp_path):
    key = OS.keygen(tmp_path / "k.pem")
    n = key.public_key().public_numbers()
    c = OS.public_c(key.public_key())
    rows = [line.strip().strip("{},").split(",") for line in c.splitlines() if line.strip().startswith("{ 0x")]
    x = sum(int(w, 16) << (32 * i) for i, w in enumerate(rows[0]))
    y = sum(int(w, 16) << (32 * i) for i, w in enumerate(rows[1]))
    assert (x, y) == (n.x, n.y)                   # 32-bit words, least significant first


# ---- abutton_ble --------------------------------------------------------------------------
def test_parse_bthome():
    ble = pytest.importorskip("abutton_ble")
    assert ble.parse_bthome(bytes.fromhex("40 00 07 01 55 3a 02")) == \
        {"packet_id": 7, "battery": 0x55, "button": "double_press"}
    assert ble.parse_bthome(bytes.fromhex("40 00 08 01 64")) == {"packet_id": 8, "battery": 100}
    assert ble.parse_bthome(bytes.fromhex("41 00 01")) is None      # encrypted
    assert ble.parse_bthome(bytes.fromhex("20 00 01")) is None      # BTHome v1
