"""Tests for research/fw_mod.py on a synthetic DS laid out like the stock one
(no dependence on the private firmware dump)."""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "research"))

import ds_tool as DT
import fw_mod as M

DATA_BASE = 0x220080  # one 0x0103 record covering both press-flash defaults


def _stock_flash() -> bytes:
    body = bytearray(0x80)
    for rec in M.PRESS_FLASH_RECORDS:
        o = rec - DATA_BASE
        body[o:o + len(M.STOCK_PRESS_FLASH)] = M.STOCK_PRESS_FLASH
    header = b"BRCMcfgD" + bytes(4) + struct.pack("<I", 0x1234)
    sec = DT.Section(header, [
        [0x0303, bytearray(b"BCM20735B0 fcbga\x00")],
        [0x0103, bytearray(struct.pack("<I", DATA_BASE) + body)],
    ])
    flash = bytearray(b"\xff" * 0x8000)
    ds = sec.to_bytes()
    flash[M.DS_OFFSET:M.DS_OFFSET + len(ds)] = ds
    return bytes(flash)


def test_press_color_recolors_both_defaults():
    flash = _stock_flash()
    sec, end = DT.Section.parse(flash, M.DS_OFFSET)
    M.set_press_color(sec, (0xFF, 0xFF, 0xFF))
    new = M.rebuild_image(flash, sec, end)
    assert len(new) == len(flash)
    sec2, _ = DT.Section.parse(new, M.DS_OFFSET)
    for rec in M.PRESS_FLASH_RECORDS:
        got = M.ram_read(sec2, rec, len(M.STOCK_PRESS_FLASH))
        # step 0 is now white; duration, step 1 (off) and the button-down trigger are unchanged
        assert got[M.STEP0_RGB:M.STEP0_RGB + 3] == b"\xff\xff\xff"
        assert got[:M.STEP0_RGB] == M.STOCK_PRESS_FLASH[:M.STEP0_RGB]
        assert got[M.STEP0_RGB + 3:] == M.STOCK_PRESS_FLASH[M.STEP0_RGB + 3:]
    # exactly 2 bytes per record changed (blue already 0xff), all in one sector
    diff = [i for i in range(len(flash)) if flash[i] != new[i]]
    assert len(diff) == 4
    assert M.changed_sectors(flash, new) == [0x4000]


def test_press_color_refuses_unknown_firmware():
    flash = bytearray(_stock_flash())
    sec, _ = DT.Section.parse(bytes(flash), M.DS_OFFSET)
    M.set_press_color(sec, (0, 255, 0))
    with pytest.raises(ValueError):  # already modified -> no longer the stock bytes
        M.set_press_color(sec, (255, 255, 255))


def test_parse_rgb():
    assert M.parse_rgb("#ffffff") == (255, 255, 255)
    assert M.parse_rgb("0000ff") == (0, 0, 255)
