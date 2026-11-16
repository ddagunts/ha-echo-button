"""Opens an RFCOMM connection without Python's AF_BLUETOOTH support. No Home Assistant imports.

Some Python builds (Home Assistant's own among them) are compiled without
Bluetooth sockets, so the socket is created and connected through libc and
handed back as a plain non-blocking file descriptor.

Connecting directly, rather than through BlueZ's profile API, means the
button's SDP server is never needed: it can stop answering (seen after an
abrupt adapter loss) while the gadget channel itself keeps working.
"""

import asyncio
import ctypes
import errno
import os
import socket

AF_BLUETOOTH = 31
BTPROTO_RFCOMM = 3

_libc = ctypes.CDLL(None, use_errno=True)  # libc is already loaded by Python itself (glibc or musl)
_libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_libc.socket.restype = ctypes.c_int
_libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libc.bind.restype = ctypes.c_int
_libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libc.connect.restype = ctypes.c_int
_libc.getsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
_libc.getsockopt.restype = ctypes.c_int


class _SockaddrRc(ctypes.Structure):
    """struct sockaddr_rc from <bluetooth/rfcomm.h>."""

    _fields_ = [("family", ctypes.c_ushort), ("bdaddr", ctypes.c_uint8 * 6), ("channel", ctypes.c_uint8)]


def sockaddr_rc(address: str, channel: int) -> _SockaddrRc:
    octets = bytes.fromhex(address.replace(":", ""))
    if len(octets) != 6:
        raise ValueError(f"not a Bluetooth address: {address}")
    # bdaddr_t is little-endian: AA:BB:CC:DD:EE:FF is stored as FF EE DD CC BB AA.
    return _SockaddrRc(AF_BLUETOOTH, (ctypes.c_uint8 * 6)(*reversed(octets)), channel)


def _check(result: int) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


async def connect(address: str, channel: int, adapter_address: str | None = None) -> int:
    """Connect to RFCOMM `channel` on `address` (through the adapter with `adapter_address`).

    Returns a connected non-blocking file descriptor; the caller closes it.
    Raises OSError (e.g. ECONNREFUSED, EHOSTDOWN when the device doesn't answer).
    Cancelling closes the socket, which also aborts the attempt in the kernel.
    """
    fd = _check(_libc.socket(AF_BLUETOOTH, socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC,
                             BTPROTO_RFCOMM))
    try:
        if adapter_address is not None:
            local = sockaddr_rc(adapter_address, 0)
            _check(_libc.bind(fd, ctypes.byref(local), ctypes.sizeof(local)))
        remote = sockaddr_rc(address, channel)
        if _libc.connect(fd, ctypes.byref(remote), ctypes.sizeof(remote)) < 0:
            err = ctypes.get_errno()
            if err != errno.EINPROGRESS:
                raise OSError(err, os.strerror(err))
            await _writable(fd)
            if err := _socket_error(fd):
                raise OSError(err, os.strerror(err))
        return fd
    except BaseException:
        os.close(fd)
        raise


async def _writable(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.add_writer(fd, lambda: None if ready.done() else ready.set_result(None))
    try:
        await ready
    finally:
        loop.remove_writer(fd)


def _socket_error(fd: int) -> int:
    value = ctypes.c_int(0)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    _check(_libc.getsockopt(fd, socket.SOL_SOCKET, socket.SO_ERROR, ctypes.byref(value), ctypes.byref(size)))
    return value.value
