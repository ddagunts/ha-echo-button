"""AButton GATT protocol encoding (custom_components/echo_button/abutton.py)."""

import pytest

from echo_button import abutton as A


def test_config_round_trip_and_layout():
    cfg = A.Config(flags=A.FLAG_BOOT_BATTERY, brightness=128, press_color=(255, 0, 16),
                   long_press_ms=1500, multi_click_ms=400, advert_interval_ms=1000, low_battery_pct=15)
    raw = cfg.encode()
    # version, flags, brightness, R G B, long LE, multi LE, advert LE, low battery
    assert raw == bytes([1, 0x02, 128, 255, 0, 16, 0xDC, 0x05, 0x90, 0x01, 0xE8, 0x03, 15])
    assert A.Config.decode(raw) == cfg
    assert A.Config().encode() == bytes([1, 0x07, 255, 255, 255, 255, 0xE8, 0x03, 0x5E, 0x01, 0xFA, 0x00, 20])


@pytest.mark.parametrize("kwargs", [
    {"brightness": 0}, {"long_press_ms": 299}, {"long_press_ms": 5001}, {"multi_click_ms": 99},
    {"advert_interval_ms": 10001}, {"low_battery_pct": 101}, {"flags": 0x08}, {"press_color": (256, 0, 0)},
])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        A.Config(**kwargs)


def test_config_decode_rejects_other_versions_and_lengths():
    raw = bytearray(A.Config().encode())
    raw[0] = 2
    with pytest.raises(ValueError, match="version"):
        A.Config.decode(bytes(raw))
    with pytest.raises(ValueError, match="bytes"):
        A.Config.decode(bytes(raw[:12]))


def test_flags():
    cfg = A.Config().with_flag(A.FLAG_RESTORE_LED, False)
    assert not cfg.flag(A.FLAG_RESTORE_LED) and cfg.flag(A.FLAG_BOOT_BATTERY)
    assert cfg.with_flag(A.FLAG_RESTORE_LED, True) == A.Config()


def test_led_color_and_stop():
    assert A.led_color((1, 2, 3)) == bytes([1, 1, 2, 3])
    assert A.led_color(None) == bytes([1, 0, 0, 0])
    assert A.led_stop() == b"\x00"


def test_animation_fits_20_byte_writes():
    steps = [A.Step(100 * (i + 1), (i, 0, 255 - i), fade=bool(i % 2)) for i in range(7)]
    writes = A.led_animation(steps, repeat=3)
    assert all(len(w) <= 20 for w in writes)
    assert writes[0][:3] == bytes([2, 3, 7])
    assert all(w[0] == 3 for w in writes[1:])
    body = writes[0][3:] + b"".join(w[1:] for w in writes[1:])
    assert body == b"".join(s.encode() for s in steps)
    assert steps[1].encode() == bytes([0x00, 0xC8, 1, 0, 254, 1])     # duration big-endian, as stock
    # a bigger MTU means fewer writes
    assert len(A.led_animation(steps, write_size=244)) == 1


def test_animation_limits():
    with pytest.raises(ValueError):
        A.led_animation([])
    with pytest.raises(ValueError):
        A.led_animation([A.Step(10, (0, 0, 0))] * 41)
    with pytest.raises(ValueError):
        A.led_animation([A.Step(0, (0, 0, 0))])
    with pytest.raises(ValueError):
        A.led_animation([A.Step(10, (0, 0, 0))], repeat=256)
    assert A.led_animation([A.Step(10, (0, 0, 0))], repeat=0)[0][:3] == bytes([2, 0, 1])   # forever


def test_led_state_and_names():
    assert A.LedState.decode(bytes([9, 8, 7, 1])) == A.LedState((9, 8, 7), True)
    assert A.check_name("Garage Button") == b"Garage Button"
    assert A.check_name("") == b""
    for bad in ("x" * 27, "café", "tab\there"):
        with pytest.raises(ValueError):
            A.check_name(bad)
    assert A.parse_hex_color("#FF8800") == (255, 136, 0)


def test_classic_records_finds_only_stale_classic_entries():
    from echo_button.bluez import ADAPTER, DEVICE, classic_records

    def dev(addr, **kw):
        return {DEVICE: {"Address": addr, **kw}}

    objects = {
        "/org/bluez/hci0": {ADAPTER: {"Address": "AA:AA:AA:AA:AA:AA"}},
        "/org/bluez/hci0/dev_00_11_22_33_44_55": dev("00:11:22:33:44:55", Paired=True, Bonded=True, Class=0x50A),
        "/org/bluez/hci1/dev_00_11_22_33_44_55": dev("00:11:22:33:44:55", Class=0x50A),   # inquiry only
        "/org/bluez/hci2/dev_00_11_22_33_44_55": dev("00:11:22:33:44:55", Paired=False),  # from BLE adverts
        "/org/bluez/hci0/dev_00_11_22_33_44_66": dev("00:11:22:33:44:66", Paired=True),   # another device
    }
    assert classic_records(objects, "00:11:22:33:44:55") == [
        ("/org/bluez/hci0", "/org/bluez/hci0/dev_00_11_22_33_44_55"),
        ("/org/bluez/hci1", "/org/bluez/hci1/dev_00_11_22_33_44_55"),
    ]
    assert classic_records(objects, "00:11:22:33:44:77") == []
