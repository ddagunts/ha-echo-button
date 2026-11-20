"""The firmware's BTHome encoder (firmware/abutton/bthome.c), compiled for the host.

Checks AES-128 against FIPS-197, AES-CCM against `cryptography`, and whole service-data
frames against bthome-ble, the decoder Home Assistant's BTHome integration uses.
"""

import ctypes
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

SRC = Path(__file__).resolve().parent.parent / "firmware" / "abutton" / "bthome.c"
BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
KEY = bytes.fromhex("231d39c1d7cc1ab1aee224cd096db932")
MAC = "54:48:E6:8F:80:A5"


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    so = tmp_path_factory.mktemp("bthome") / "bthome.so"
    subprocess.run([cc, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(so), str(SRC)],
                   check=True)
    return ctypes.CDLL(str(so))


def aes(lib, key: bytes, block: bytes) -> bytes:
    out = ctypes.create_string_buffer(16)
    lib.aes128_encrypt(key, block, out)
    return out.raw


def ccm(lib, key: bytes, nonce: bytes, data: bytes) -> bytes:
    out, mic = ctypes.create_string_buffer(max(len(data), 1)), ctypes.create_string_buffer(4)
    lib.aes128_ccm4_encrypt(key, nonce, data, len(data), out, mic)
    return out.raw[:len(data)] + mic.raw


def service_data(lib, objs: bytes, key: bytes | None, mac: str = MAC, counter: int = 0) -> bytes:
    out = ctypes.create_string_buffer(64)
    n = lib.bthome_service_data(out, objs, len(objs), key, bytes.fromhex(mac.replace(":", "")),
                                ctypes.c_uint32(counter))
    return out.raw[:n]


def test_aes128_fips197(lib):
    key = bytes(range(16))
    assert aes(lib, key, bytes.fromhex("00112233445566778899aabbccddeeff")).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"


@pytest.mark.parametrize("length", [0, 1, 4, 6, 15, 16, 17, 31, 32, 33, 100])
def test_ccm_matches_cryptography(lib, length):
    key, nonce, data = os.urandom(16), os.urandom(13), os.urandom(length)
    assert ccm(lib, key, nonce, data) == AESCCM(key, tag_length=4).encrypt(nonce, data, None)


def test_plain_frame_layout(lib):
    assert service_data(lib, bytes([0x00, 0x07, 0x01, 0x64]), None).hex() == "d2fc4000070164"


def test_encrypted_frame_layout(lib):
    objs = bytes([0x00, 0x07, 0x01, 0x64, 0x3A, 0x01])
    sd = service_data(lib, objs, KEY, counter=0x01020304)
    assert sd[:3] == bytes([0xD2, 0xFC, 0x41])
    assert len(sd) == 3 + len(objs) + 8
    assert sd[-8:-4] == bytes([4, 3, 2, 1])                      # counter, little-endian
    nonce = bytes.fromhex(MAC.replace(":", "")) + bytes([0xD2, 0xFC, 0x41]) + sd[-8:-4]
    assert AESCCM(KEY, tag_length=4).decrypt(nonce, sd[3:-8] + sd[-4:], None) == objs


def _bthome():
    return pytest.importorskip("bthome_ble")


def _info(sd: bytes, t: float):
    from habluetooth import BluetoothServiceInfoBleak

    return BluetoothServiceInfoBleak(name="AButton", address=MAC, rssi=-60, manufacturer_data={},
                                     service_data={BTHOME_UUID: sd[2:]}, service_uuids=[BTHOME_UUID],
                                     source="local", device=None, advertisement=None, connectable=True,
                                     time=t, tx_power=None)


def test_bthome_ble_decodes_encrypted_press(lib):
    bthome = _bthome()
    from bthome_ble.parser import EncryptionScheme
    dev = bthome.BTHomeBluetoothDeviceData(bindkey=KEY)
    update = dev.update(_info(service_data(lib, bytes([0x00, 0x07, 0x01, 0x5A, 0x3A, 0x02]), KEY, counter=500), 1.0))
    assert dev.bindkey_verified
    assert dev.encryption_scheme == EncryptionScheme.BTHOME_BINDKEY
    values = {k.key: v.native_value for k, v in update.entity_values.items()}
    assert values["battery"] == 90
    assert [e.event_type for e in update.events.values()] == ["double_press"]


def test_bthome_ble_rejects_wrong_key_and_downgrade(lib):
    bthome = _bthome()
    objs = bytes([0x00, 0x08, 0x01, 0x5A, 0x3A, 0x01])
    wrong = bthome.BTHomeBluetoothDeviceData(bindkey=bytes(16))
    for t in (1.0, 2.0):                          # it gives up on the key after two failures
        update = wrong.update(_info(service_data(lib, objs, KEY, counter=600 + int(t)), t))
        assert not update.events
    assert not wrong.bindkey_verified
    dev = bthome.BTHomeBluetoothDeviceData(bindkey=KEY)
    assert not dev.update(_info(service_data(lib, objs, None), 1.0)).events   # plaintext ignored


def test_bthome_ble_needs_a_rising_counter(lib):
    """Why the firmware keeps its counter across reboots: a lower counter (>= 100) is dropped."""
    bthome = _bthome()
    dev = bthome.BTHomeBluetoothDeviceData(bindkey=KEY)
    dev.update(_info(service_data(lib, bytes([0x00, 0x01, 0x3A, 0x01]), KEY, counter=1000), 1.0))
    replay = dev.update(_info(service_data(lib, bytes([0x00, 0x02, 0x3A, 0x01]), KEY, counter=900), 10.0))
    assert not replay.events
    later = dev.update(_info(service_data(lib, bytes([0x00, 0x03, 0x3A, 0x01]), KEY, counter=1001), 20.0))
    assert [e.event_type for e in later.events.values()] == ["press"]


def test_without_a_key_bthome_ble_sees_an_encrypted_device(lib):
    """Home Assistant then asks for the key (its BTHome integration starts a reauth)."""
    bthome = _bthome()
    from bthome_ble.parser import EncryptionScheme
    dev = bthome.BTHomeBluetoothDeviceData()
    dev.update(_info(service_data(lib, bytes([0x00, 0x01, 0x3A, 0x01]), KEY, counter=5), 1.0))
    assert dev.encryption_scheme == EncryptionScheme.BTHOME_BINDKEY
    assert not dev.bindkey_verified


def test_abutton_ble_monitor_decrypts_firmware_frames(lib):
    import sys

    sys.path.insert(0, str(SRC.parent.parent))
    ble = pytest.importorskip("abutton_ble")
    sd = service_data(lib, bytes([0x00, 0x09, 0x01, 0x4B, 0x3A, 0x04]), KEY, counter=77)[2:]
    assert ble.parse_bthome(sd, MAC, KEY) == {"counter": 77, "packet_id": 9, "battery": 75, "button": "long_press"}
    assert ble.parse_bthome(sd, MAC, bytes(range(16))) == {"encrypted": "wrong key"}
    assert ble.parse_bthome(sd, "00:00:00:00:00:01", KEY) == {"encrypted": "wrong key"}   # nonce has the MAC
    assert ble.parse_bthome(sd) == {"encrypted": "no key"}
    plain = service_data(lib, bytes([0x00, 0x09, 0x01, 0x4B]), None)[2:]
    assert ble.parse_bthome(plain) == {"packet_id": 9, "battery": 75}
